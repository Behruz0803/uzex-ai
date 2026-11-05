# -*- coding: utf-8 -*-
"""
UZEX Text-to-SQL Agent with Anti-Hallucination
- Uses Gemini to generate SQL from natural language (UZ/RU)
- Executes SQL safely
- Formats answer with proof (SQL + calculation)
- If 0 rows → says no data in user's language (critical for disqualification avoidance)

Designed for hackathon: 10 live questions, 90s per answer, 60+20+20 scoring.
"""
import os
import re
import json
import time
import asyncio
import random
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

from .schema import SCHEMA_DESCRIPTION, FEW_SHOT_EXAMPLES
from .db import execute_sql, is_safe_select, search_entities, entities_context
from .textnorm import prepare_sql, search_hints

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
MODEL_FALLBACKS = [
    MODEL,
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-2.0-flash",
]
_client = None

def get_client():
    global _client
    if _client is None:
        api_key = (os.getenv("UZEX_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("UZEX_GEMINI_API_KEY not set. Add to .env or Vercel env.")
        _client = genai.Client(api_key=api_key, http_options={"timeout": 180000})
    return _client

# Uzbek/Russian detection heuristic (audit fix #2: never default to RU for
# ambiguous short queries like "BK001"; default to UZ in the exchange context).
def detect_language(text: str) -> str:
    t = (text or "").lower()
    uz_markers = ["qancha", "qaysi", "qachon", "kim", "nima", "qayer", "necha", "nechta",
                  "uchun", "bo'yicha", "boyicha", "narxi", "narx", "bitim", "shartnoma",
                  "xarid", "sotuvchi", "xaridor", "savdo", "aylanma", "viloyat", "hudud",
                  "yilda", "yili", "jami", "o'rtacha", "ortacha", "eng", "ko'p", "kop",
                  "qimmat", "arzon", "sement", "bug'doy", "paxta", "yog'", "guruch", "un",
                  "shakar", "benzin", "dizel", "g'isht", "polat", "alyuminiy", "mis"]
    ru_markers = ["сколько", "какой", "какая", "когда", "кто", "что", "где", "цена", "сделк",
                  "контракт", "брокер", "покупатель", "продавец", "товар", "оборот", "сумма",
                  "средний", "самый", "дорогой", "дешев", "регион", "област", "год", "цемент",
                  "пшениц", "хлопок", "масло", "рис", "сахар", "бензин", "дизел", "кирпич"]
    uz_score = sum(1 for w in uz_markers if w in t)
    ru_score = sum(1 for w in ru_markers if w in t)
    has_cyrillic = bool(re.search(r"[а-яё]", t))
    has_uz_apos = bool(re.search(r"[og]\s*[\u02bb\u2018\u2019'`]", t))
    if has_uz_apos:
        uz_score += 2
    if uz_score > ru_score:
        return "uz"
    if ru_score > uz_score:
        return "ru"
    # tie / no markers: Cyrillic -> ru, otherwise default UZ (audit fix #2)
    return "ru" if has_cyrillic else "uz"


# ---------------------------------------------------------------------------
# Near-match (team request): if the EXACT conditions match nothing but a
# broadened query finds data, say so explicitly instead of inventing an answer.
# ---------------------------------------------------------------------------
NEAR_PREFIX_UZ = ("Aynan sizning shartlaringizga to'g'ri keladigan ma'lumot topilmadi, "
                  "lekin shunga yaqin bir nechta natija bor:")
NEAR_PREFIX_RU = ("Данных, точно соответствующих вашим условиям, не найдено, "
                  "но есть несколько близких результатов:")

_RELAX_PATTERNS = [
    # month range (same year-month)
    (r"deal_date\s+BETWEEN\s+'(\d{4})-(\d{2})-\d{2}'\s+AND\s+'\1-\2-\d{2}'",
     "oy filtri olib tashlandi", "убран фильтр месяца"),
    # full-year range
    (r"deal_date\s+BETWEEN\s+'\d{4}-01-01'\s+AND\s+'\d{4}-12-31'",
     "yil filtri olib tashlandi", "убран фильтр года"),
    (r"strftime\('%Y',\s*deal_date\)\s*=\s*'\d{4}'",
     "yil filtri olib tashlandi", "убран фильтр года"),
    (r"deal_date\s+LIKE\s+'\d{4}-%'",
     "yil filtri olib tashlandi", "убран фильтр года"),
    # any other date range
    (r"deal_date\s+BETWEEN\s+'[^']+'\s+AND\s+'[^']+'",
     "sana oralig'i olib tashlandi", "убран диапазон дат"),
    # region filters (ufold-aware or plain)
    (r"(?:ufold\()?(?:\w+\.)?region\)?\s*(?:NOT\s+)?LIKE\s*(?:ufold\()?'[^']+'\)?",
     "hudud filtri olib tashlandi", "убран фильтр региона"),
    (r"(?:\w+\.)?region\s*=\s*'[^']+'",
     "hudud filtri olib tashlandi", "убран фильтр региона"),
]


def relax_sql(sql: str):
    """Deterministically broaden a no-data query by removing ONE constraint.

    Returns (relaxed_sql, (uz_note, ru_note)) or (None, None).
    """
    if not sql:
        return None, None
    for pat, uz_note, ru_note in _RELAX_PATTERNS:
        m = re.search(pat, sql, re.IGNORECASE)
        if not m:
            continue
        # Remove the matched condition together with its leading WHERE/AND.
        start = m.start()
        prefix = sql[:start]
        # find the connector right before the condition
        conn = re.search(r"(\bWHERE|\bAND)\s*$", prefix, re.IGNORECASE)
        if conn:
            new = sql[:conn.start()] + sql[m.end():]
            # if we removed a WHERE that still has following conditions, keep WHERE
            if conn.group(1).upper() == "WHERE":
                rest = sql[m.end():].lstrip()
                if rest and not re.match(r"(GROUP|ORDER|LIMIT|;|$)", rest, re.IGNORECASE):
                    new = sql[:conn.start()] + "WHERE " + sql[m.end():].lstrip()
        else:
            new = sql[:start] + sql[m.end():]
        new = re.sub(r"WHERE\s+AND\s+", "WHERE ", new, flags=re.IGNORECASE)
        new = re.sub(r"WHERE\s*(GROUP|ORDER|LIMIT|;|$)", r"\1", new, flags=re.IGNORECASE)
        new = re.sub(r"\bAND\s+AND\b", "AND", new, flags=re.IGNORECASE)
        new = re.sub(r"[ \t]{2,}", " ", new).strip()
        if new and new != sql:
            return new, (uz_note, ru_note)
    return None, None


SYSTEM_PROMPT = f"""
Sen — UZEX AI Agent, O'zbekiston Tovar-Xomashyo Birjasi ma'lumotlari bo'yicha mutaxassisan.
Ты — UZEX AI Agent, эксперт по данным Товарно-сырьевой биржи Узбекистана.

CRITICAL RULES (DISQUALIFICATION RISK):
- NEVER hallucinate: never invent INN, company name, product name, broker code, price, count.
- If SQL returns 0 rows → you MUST say in user's language: "Ma'lumotlar bazasida bunday ma'lumot mavjud emas" (UZ) or "В базе данных такой информации нет" (RU). Do NOT make up numbers.
- Always generate valid SQLite SELECT query. Only SELECT/WITH allowed. No INSERT/UPDATE/DELETE.
- Use LIKE with COLLATE NOCASE for product names (they are dirty: "1 сорт" vs "1-сорт").
- For date filters: user says "2024 йилда" / "в 2024 году" → use BETWEEN '2024-01-01' AND '2024-12-31' or strftime('%Y', deal_date)='2024'.
- For export sums in UZS: JOIN deals -> contracts -> usd_rates, calculate CASE WHEN currency='Доллар США' THEN deal_sum*rate ELSE deal_sum END.
- For unit price: deal_sum / (quantity * lot). For volume: quantity * lot.
- Always add LIMIT 100 unless aggregation (COUNT/SUM/AVG...).
- Respond in user's language (UZ if question is Uzbek, RU if Russian).
- Output MUST be valid JSON with keys: "sql", "explanation", "language" (uz/ru), "needs_data" (true/false).

{SCHEMA_DESCRIPTION}

{FEW_SHOT_EXAMPLES}

RESPONSE FORMAT (strict JSON):
{{
  "sql": "SELECT ...",
  "explanation": "Qisqa izoh / Краткое объяснение: nima hisoblandi va qaysi formula",
  "language": "uz" or "ru",
  "needs_data": true
}}

If question is unrelated to DB (e.g., "Salom", "Kimsan?") → set needs_data=false and sql="".
Short market questions are NOT greetings. Always return SQL:
- "eng qimmat bitim qaysi" → MAX deal_sum_uzs, status Исполнен, no year means all years
- "eng arzonichi" / "eng arzon" → MIN unit_price_uzs among executed deals
- "pul aylantirgan brokerlar" → SUM(deal_sum_uzs) by broker, both sides, no double count
- "2026 avgust top brokerlar" → COUNT deals in that month only
- "aksiya" / "акция" → search product_name; 0 rows is correct, do not invent a share price
- "2026 yil 3-avgust 1$lik shartnoma" is a DATABASE question, never a greeting.
  Date 2026-08-03, amount 1, currency Доллар США.
  Search contracts.base_price, deals.deal_sum and unit price. 0 rows is correct if none match.
  Do not invent a $1 contract. "shartoma" is a typo of shartnoma.

Example hallucination prevention:
User: "Kompaniya 'SpaceX' ning 2024 yildagi bitimlari qancha?"
You generate: SELECT ... WHERE p.name LIKE '%SpaceX%' → executes → 0 rows → you answer "Ma'lumotlar bazasida bunday ma'lumot mavjud emas (SpaceX nomi bilan kompaniya topilmadi). [SQL: ...]"
NEVER say "SpaceX 5 ta bitim qilgan".

Always show SQL and calculation basis in final answer — this is 20 балла за качество прототипа.
"""

async def generate_sql(question: str) -> dict:
    """Ask Gemini to generate SQL JSON, with heuristic fallback if no API key / no internet"""
    lang_hint = detect_language(question)
    # Qisqa og'zaki savolni Gemini salom deb o'qimasin — aniq naqsh bo'lsa darhol SQL.
    try:
        from .heuristic import heuristic_sql
        sure = heuristic_sql(question)
        if sure.get("confident") and sure.get("sql"):
            return sure
    except Exception as e:
        print(f"confident heuristic error: {e}")
    # Try heuristic first if no API key or heuristic can handle quickly and Gemini not reachable
    api_key = os.getenv("UZEX_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    # If no API key, use heuristic directly
    if not api_key:
        try:
            from .heuristic import heuristic_sql
            h = heuristic_sql(question)
            if h.get("sql"):
                return h
            # If heuristic says needs_data False but question looks like DB query, still try heuristic
            # Fallback to greeting
            if not h.get("sql"):
                return h
        except Exception as e:
            print(f"heuristic error: {e}")
        return {"sql": "", "explanation": "GEMINI_API_KEY o'rnatilmagan — heuristic ishlatildi, lekin mos SQL topilmadi", "language": lang_hint, "needs_data": False}

    # Try heuristic as fast path for common patterns (optional, but keep Gemini as primary)
    # We will still go to Gemini for accuracy, unless heuristic is confident
    # Uncomment to prioritize heuristic for speed:
    # from .heuristic import heuristic_sql
    # h = heuristic_sql(question)
    # if h.get("sql"):
    #     return h

    try:
        client = get_client()
    except Exception as e:
        # fallback to heuristic if client creation fails
        print(f"Gemini client error, using heuristic: {e}")
        from .heuristic import heuristic_sql
        h = heuristic_sql(question)
        return h

    user_prompt = f"""
Savol / Вопрос: "{question}"

Til / Язык (auto-detected): {lang_hint}
Talab: Yuqoridagi schema asosida to'g'ri SQLite SELECT so'rovini JSON formatda tuz.
Требование: Составь корректный SQLite SELECT на основе схемы выше, в формате JSON.

Return ONLY JSON, no markdown, no extra text.

{search_hints(question)}

Bazadan topilgan aniq entity'lar / Найденные в базе сущности (grounding):
{entities_context(search_entities(question)) or "(aniq entity topilmadi / точных сущностей не найдено)"}
"""
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.1,  # low temp for deterministic SQL
        max_output_tokens=2000,
    )
    try:
        resp = None
        last_err = None
        # Audit fix #1: retry with exponential backoff + model fallback.
        # 503/429/timeout are transient -> retry; auth errors -> stop early.
        models = [m for m in dict.fromkeys(MODEL_FALLBACKS) if m]
        max_rounds = int(os.getenv("GEMINI_RETRY_ROUNDS", "3"))
        for round_no in range(max_rounds):
            for model_name in models:
                try:
                    resp = await client.aio.models.generate_content(
                        model=model_name,
                        contents=[types.Content(role="user", parts=[types.Part(text=user_prompt)])],
                        config=config
                    )
                    print(f"Gemini SQL model: {model_name} (round {round_no + 1})")
                    break
                except Exception as e:
                    last_err = e
                    msg = str(e).lower()
                    print(f"⚠️ Gemini model {model_name} failed: {e}")
                    # Hard auth/permission errors won't be fixed by retrying.
                    if any(k in msg for k in ("api key", "permission", "unauthenticated", "billing", "403", "401")):
                        break
            if resp is not None:
                break
            # Exponential backoff with jitter before the next round (503/429).
            if round_no < max_rounds - 1:
                delay = min(8.0, 1.5 * (2 ** round_no)) + random.uniform(0, 0.5)
                print(f"⏳ Gemini backoff {delay:.1f}s before retry round {round_no + 2}")
                await asyncio.sleep(delay)
        if resp is None:
            raise last_err or RuntimeError("Gemini javob bermadi")
        text = (resp.text or "").strip()
        # Clean markdown code fences if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()
        # Try parse JSON
        try:
            data = json.loads(text)
            # Validate keys
            if "sql" not in data:
                # Try extract SQL from text
                m = re.search(r"SELECT[\s\S]*?;", text, re.IGNORECASE)
                if m:
                    data = {"sql": m.group(0), "explanation": data.get("explanation", ""), "language": lang_hint, "needs_data": True}
                else:
                    raise ValueError("No sql key")
            # Normalize
            data["language"] = data.get("language", lang_hint)
            if data["language"] not in ("uz", "ru"):
                data["language"] = lang_hint
            return data
        except Exception as e:
            # Fallback: try to extract SQL via regex
            print(f"⚠️ JSON parse failed: {e}, raw: {text[:500]}")
            m = re.search(r"(SELECT[\s\S]+?)(?:;|\n\n|$)", text, re.IGNORECASE)
            if m:
                sql = m.group(1).strip()
                if not sql.endswith(";"):
                    sql += ";"
                return {"sql": sql, "explanation": "Savol bo'yicha bazadan aniq SELECT bajarildi. Raqamlar faqat shu so'rov natijasi.", "language": lang_hint, "needs_data": True}
            # Try heuristic as backup
            from .heuristic import heuristic_sql
            h = heuristic_sql(question)
            if h.get("sql"):
                return h
            return {"sql": "", "explanation": f"SQL yaratishda xato: {e}", "language": lang_hint, "needs_data": False, "error": text[:500]}
    except Exception as e:
        print(f"⚠️ Gemini API error, falling back to heuristic: {e}")
        from .heuristic import heuristic_sql
        h = heuristic_sql(question)
        if h.get("sql"):
            return h
        return {"sql": "", "explanation": f"Gemini xatosi: {e}", "language": lang_hint, "needs_data": False}

def _is_aggregation_no_data(rows, columns, rowcount):
    """Detect aggregation that returned 0/NULL as no-data (e.g., COUNT=0, SUM=NULL)"""
    if rowcount != 1 or not rows:
        return False
    row = rows[0]
    # Check if any count column is 0
    for col in columns:
        if col.lower() in ("cnt", "count", "cnt_all", "count(*)", "count(*)"):
            try:
                if int(row[col]) == 0:
                    return True
            except:
                pass
        # Also check generic cnt
        if "cnt" in col.lower():
            try:
                if int(row[col]) == 0:
                    # If there are other numeric columns that are NULL/0, it's likely no-data
                    # Check if all other numeric columns are None/0
                    numeric_cols = [c for c in columns if c not in ("cnt",)]
                    all_empty = True
                    for c2 in columns:
                        v = row[c2]
                        if v is not None and v != 0:
                            # If any column has non-zero/non-null besides cnt, it's valid data (e.g., cnt=0 but total=0 is still no-data)
                            # But if total_sum is not null, then there is data
                            if isinstance(v, (int, float)) and v != 0:
                                all_empty = False
                                break
                            elif isinstance(v, str) and v.strip() not in ("", "—", "0"):
                                all_empty = False
                                break
                    if all_empty:
                        return True
            except:
                pass
    # MIN/MAX of an empty match returns one row of NULLs — that is not a result
    if rows and all(row[c] is None for c in columns):
        return True
    # Check for single-row aggregation where SUM is NULL and COUNT 0
    if "cnt" in [c.lower() for c in columns]:
        for rkey in ["total", "total_sum", "sum", "deal_sum", "total_uzs"]:
            for col in columns:
                if rkey in col.lower():
                    if row[col] is None:
                        # If count 0 and sum null -> no data
                        cnt_val = None
                        for c in columns:
                            if "cnt" in c.lower():
                                cnt_val = row[c]
                                break
                        if cnt_val == 0:
                            return True
    return False

def format_answer(question: str, sql: str, rows, columns, rowcount, error, explanation, lang, elapsed_ms):
    """Format final answer with table + proof"""
    # Language-specific no-data message (critical!)
    no_data_uz = "Ma'lumotlar bazasida bunday ma'lumot mavjud emas."
    no_data_ru = "В базе данных такой информации нет."

    # Build header
    if error:
        # Check if error is due to SQLite syntax vs empty
        # If SQL was safe but returned error, inform
        if "0 rows" in error or rowcount == 0:
            pass  # handled below
        else:
            # SQL execution error
            if lang == "uz":
                return f"⚠️ So'rovni bajarishda xato: {error}\n\n**SQL:**\n```sql\n{sql}\n```\n\n**Izoh:** {explanation}\n\n⏱ {elapsed_ms} ms"
            else:
                return f"⚠️ Ошибка выполнения запроса: {error}\n\n**SQL:**\n```sql\n{sql}\n```\n\n**Пояснение:** {explanation}\n\n⏱ {elapsed_ms} ms"

    # Also treat aggregation no-data as 0
    is_agg_empty = _is_aggregation_no_data(rows, columns, rowcount) if rows else False
    if rowcount == 0 or not rows or is_agg_empty:
        if lang == "uz":
            base = f"{no_data_uz}"
            detail = f"\n\n**Tekshirilgan SQL:**\n```sql\n{sql}\n```\n\n**Izoh:** {explanation}\n\n*Ushbu javob aniq ma'lumotlar bazasi asosida berildi — agar ma'lumot topilmasa, to'qib chiqarilmaydi.*"
            return base + detail + f"\n\n⏱ {elapsed_ms} ms | 0 qator"
        else:
            base = f"{no_data_ru}"
            detail = f"\n\n**Проверенный SQL:**\n```sql\n{sql}\n```\n\n**Пояснение:** {explanation}\n\n*Ответ основан строго на базе данных — если данных нет, они не выдумываются.*"
            return base + detail + f"\n\n⏱ {elapsed_ms} ms | 0 строк"

    # Has data: format table
    # Build markdown table
    table_md = ""
    # If few columns and many rows, limit display
    max_rows_display = 20
    display_rows = rows[:max_rows_display]
    
    # Header
    table_md += "| " + " | ".join(columns) + " |\n"
    table_md += "| " + " | ".join(["---"] * len(columns)) + " |\n"
    for r in display_rows:
        vals = []
        for c in columns:
            v = r[c]
            if v is None:
                vals.append("—")
            elif isinstance(v, float):
                # Format numbers with space as thousand sep? Use Uzbek style: 1 234 567.89
                # But keep simple
                if v > 1000:
                    vals.append(f"{v:,.2f}".replace(",", " "))
                else:
                    vals.append(f"{v:.2f}" if v != int(v) else str(int(v)))
            else:
                # Escape pipe
                s = str(v).replace("|", "\\|")
                # Truncate long product names
                if len(s) > 60:
                    s = s[:57] + "..."
                vals.append(s)
        table_md += "| " + " | ".join(vals) + " |\n"
    if len(rows) > max_rows_display:
        table_md += f"\n*... va yana {len(rows)-max_rows_display} qator (jami {rowcount}) | ... и ещё {len(rows)-max_rows_display} строк (всего {rowcount})*"

    # Determine calc basis
    calc_note_uz = """
**Hisob-kitob va manba:**
- Hajm = Soni * Lot
- Birlik narxi = Summa / (Soni * Lot)
- Eksport (USD) -> so'm: Summa * USD kursi (usd_rates, bitim sanasi bo'yicha)
- Manba: deals (bitimlar) + contracts (shartnomalar) + usd_rates + participants/brokers
"""
    calc_note_ru = """
**Расчёт и источник:**
- Объём = Количество * Лот
- Цена за единицу = Сумма / (Количество * Лот)
- Экспорт (USD) -> сум: Сумма * курс USD (usd_rates по дате сделки)
- Источник: deals + contracts + usd_rates + participants/brokers
"""

    calc_note = calc_note_uz if lang == "uz" else calc_note_ru
    intro_uz = f"✅ **Natija ({rowcount} qator):**"
    intro_ru = f"✅ **Результат ({rowcount} строк):**"

    intro = intro_uz if lang == "uz" else intro_ru

    zero_note = ""
    numeric_vals = []
    for r in rows:
        for c in columns:
            v = r[c]
            if isinstance(v, (int, float)):
                numeric_vals.append(v)
    if numeric_vals and all(v == 0 for v in numeric_vals):
        zero_note = (
            "\n\nBu 0 — bazada bunday yozuv yo'q. Raqam to'qib chiqarilmaydi."
            if lang == "uz"
            else "\n\nЭто 0: в базе такой записи нет. Число не выдумывается."
        )

    footer = f"\n\n**SQL:**\n```sql\n{sql}\n```\n\n**Izoh:** {explanation}" if lang=="uz" else f"\n\n**SQL:**\n```sql\n{sql}\n```\n\n**Пояснение:** {explanation}"

    timing = f"\n\n⏱ {elapsed_ms} ms"

    return f"{intro}\n\n{table_md}{calc_note}{zero_note}{footer}{timing}"

async def answer(question: str, user_id=None):
    """Main entry: question -> formatted answer (with anti-hallucination)"""
    start = time.time()
    # Step 1: LLM generates SQL
    gen = await generate_sql(question)
    sql = gen.get("sql", "").strip()
    explanation = gen.get("explanation", "")
    lang = gen.get("language", detect_language(question))
    needs_data = gen.get("needs_data", True)

    # Gemini bo'sh SQL qaytarsa ham, og'zaki savolni salom deb yopma.
    if not sql:
        try:
            from .heuristic import heuristic_sql
            h = heuristic_sql(question)
            if h.get("sql"):
                sql = h["sql"]
                explanation = h.get("explanation") or explanation
                lang = h.get("language") or lang
                needs_data = True
        except Exception as e:
            print(f"heuristic after empty SQL: {e}")

    # If LLM says no data needed (greeting etc.), return direct answer
    if not needs_data or not sql:
        # For greetings, answer helpfully in user's language
        if not sql:
            try:
                from .heuristic import looks_like_market
                if looks_like_market(question):
                    if lang == "uz":
                        return (
                            "Bu birja savoli, lekin aniq SQL tuza olmadim. "
                            "Sana, summa va valyutani yozing. Masalan: 2026-08-03, 1 dollarlik shartnoma. "
                            "Raqam to'qilmaydi."
                        )
                    return (
                        "Это вопрос к бирже, но точный SQL не собрался. "
                        "Напишите дату, сумму и валюту, например: 2026-08-03, контракт на 1 доллар."
                    )
            except Exception as e:
                print(f"market guard: {e}")
            # Try to use LLM to answer directly without DB
            if lang == "uz":
                return "Salom! Men UZEX birja ma'lumotlari bo'yicha AI yordamchiman. Savol bering, masalan:\n• 2024-yilda eng ko'p bitim qilgan broker kim?\n• STIR 123456789 bo'yicha jami savdo hajmi qancha?\n• Sement mahsuloti bo'yicha eng qimmat bitim qaysi?\n\nSavolingizni yozing — men bazadan aniq javob topib, hisob-kitobi bilan ko'rsataman."
            else:
                return "Привет! Я — UZEX AI-ассистент по данным биржи. Задайте вопрос, например:\n• Какой брокер провел больше всего сделок в 2024 году?\n• Какой товар самый дорогой в 2025?\n• Сколько сделок по ИНН 123456789?\n\nНапишите вопрос — я найду точный ответ в базе с расчётом и источником."

    # Unicode LIKE + known product-name fixes (Cyrillic case, apostrophe, cotton oil)
    sql = prepare_sql(sql, question)

    # Validate SQL safety
    safe, msg = is_safe_select(sql)
    if not safe:
        elapsed = int((time.time()-start)*1000)
        if lang == "uz":
            return f"⚠️ SQL xavfsizlik tekshiruvidan o'tmadi: {msg}\n\n**Yaratilgan SQL:**\n```sql\n{sql}\n```\n⏱ {elapsed} ms"
        else:
            return f"⚠️ SQL не прошёл проверку безопасности: {msg}\n\n**Сгенерированный SQL:**\n```sql\n{sql}\n```\n⏱ {elapsed} ms"

    # Step 2: Execute SQL
    rows, columns, rowcount, error = execute_sql(sql, limit=100)

    # Step 2b: Near-match relaxation (team request). If the EXACT conditions
    # matched nothing, try a broadened query and, if it finds data, present it
    # explicitly as CLOSE results — never as an exact answer, never invented.
    if not error:
        no_data = (rowcount == 0 or not rows
                   or _is_aggregation_no_data(rows, columns, rowcount))
        if no_data:
            relaxed_sql, notes = relax_sql(sql)
            if relaxed_sql:
                r_rows, r_cols, r_cnt, r_err = execute_sql(relaxed_sql, limit=100)
                r_has_data = (not r_err and r_cnt > 0 and r_rows
                              and not _is_aggregation_no_data(r_rows, r_cols, r_cnt))
                if r_has_data:
                    elapsed = int((time.time()-start)*1000)
                    note = notes[0] if lang == "uz" else notes[1]
                    prefix = NEAR_PREFIX_UZ if lang == "uz" else NEAR_PREFIX_RU
                    relax_label = ("Qidiruv shartlari kengaytirildi" if lang == "uz"
                                   else "Условия поиска расширены")
                    body = format_answer(question, relaxed_sql, r_rows, r_cols, r_cnt,
                                         None, explanation, lang, elapsed)
                    return (f"{prefix}\n{relax_label}: {note}.\n\n{body}")

    elapsed = int((time.time()-start)*1000)

    # Step 3: Format with anti-hallucination
    # If error is truncation note, it's not fatal
    if error and "truncated" in error.lower():
        # Still has rows, show warning
        formatted = format_answer(question, sql, rows, columns, rowcount, None, explanation, lang, elapsed)
        formatted += f"\n\n⚠️ *{error}*"
        return formatted
    elif error:
        return format_answer(question, sql, rows, columns, rowcount, error, explanation, lang, elapsed)
    else:
        return format_answer(question, sql, rows, columns, rowcount, None, explanation, lang, elapsed)

# Sync wrapper for testing
def answer_sync(question: str):
    import asyncio
    return asyncio.run(answer(question))
