"""
UZEX AI — Text-to-SQL agent with anti-hallucination guardrails.

Fixes implemented (from the 05.11.2026 audit):
  #1  Gemini 503/429  -> retry with exponential backoff + model fallback chain
                       + deterministic local engine when the API is down.
  #2  Language detection -> Cyrillic/Uzbek scoring, default UZ (never RU by
                       default), per-user language memory.
  #3  Gibberish / injection -> off-topic guard + NO_QUERY rule + read-only
                       single-SELECT validation; answers forced to UZ/RU.
  #5  Entity search -> all FTS tables + deal-number regex (see db_helper).

New behaviour requested by the team:
  * If the EXACT conditions match nothing but a relaxed query finds data, the
    agent explicitly says so ("Aynan sizning shartlaringizga ... topilmadi,
    lekin shunga yaqin natijalar bor") instead of inventing an answer.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

import db_helper as db
from fallback_engine import FallbackEngine

log = logging.getLogger("uzex.agent")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-flash-lite-latest,gemini-3.5-flash-lite").split(",") if m.strip()]

# ---------------------------------------------------------------------------
# i18n strings
# ---------------------------------------------------------------------------
STRINGS = {
    "uz": {
        "no_data": "Ma'lumotlar bazasida bunday ma'lumot mavjud emas.",
        "coverage": "Bazadagi ma'lumotlar {min_d} — {max_d} oralig'ini qamrab oladi ({n} ta bitim).",
        "near_prefix": ("Aynan sizning shartlaringizga to'g'ri keladigan ma'lumot topilmadi, "
                        "lekin shunga yaqin bir nechta natija bor:"),
        "off_topic": ("Men UZEX birja bitimlari bo'yicha tahliliy agentman. "
                      "Iltimos, birja bitimlari, kompaniyalar, brokerlar, tovarlar, "
                      "narxlar yoki hududlar haqida savol bering.\n\n"
                      "Masalan: «2024-yilda jami qancha so'mlik bitim tuzilgan?»"),
        "greeting": ("Assalomu alaykum! Men UZEX birja bitimlari bo'yicha AI-tahlilchiman.\n\n"
                     "Bazada 2022–2026-yillardagi 500 000 ta bitim mavjud. "
                     "O'zbek yoki rus tilida savol bering — men aniq raqamlar va "
                     "SQL so'rov bilan javob beraman.\n\n"
                     "Masalan: «Eng ko'p bitim o'tkazgan TOP 3 broker qaysilar?»"),
        "api_down": ("Sun'iy intellekt xizmati vaqtincha band. Men javobni mahalliy "
                     "tahlil mexanizmi orqali hisobladim."),
        "source": "Manba: UZEX bitimlar reyestri",
        "sql_label": "SQL so'rov",
        "relaxed_note": "Qidiruv shartlari kengaytirildi: {what}",
        "rows_note": "{n} ta natija ko'rsatildi",
    },
    "ru": {
        "no_data": "В базе данных такой информации нет.",
        "coverage": "Данные в базе охватывают период {min_d} — {max_d} ({n} сделок).",
        "near_prefix": ("Данных, точно соответствующих вашим условиям, не найдено, "
                        "но есть несколько близких результатов:"),
        "off_topic": ("Я — аналитический агент по биржевым сделкам UZEX. "
                      "Пожалуйста, задайте вопрос о сделках, компаниях, брокерах, "
                      "товарах, ценах или регионах.\n\n"
                      "Например: «Какой общий объём сделок за 2024 год?»"),
        "greeting": ("Здравствуйте! Я AI-аналитик по биржевым сделкам UZEX.\n\n"
                     "В базе 500 000 сделок за 2022–2026 годы. Задайте вопрос на "
                     "узбекском или русском — я отвечу точными цифрами и SQL-запросом.\n\n"
                     "Например: «Какие ТОП-3 брокера провели больше всего сделок?»"),
        "api_down": ("Сервис ИИ временно перегружен. Я рассчитал ответ с помощью "
                     "локального механизма анализа."),
        "source": "Источник: реестр сделок UZEX",
        "sql_label": "SQL-запрос",
        "relaxed_note": "Условия поиска расширены: {what}",
        "rows_note": "Показано результатов: {n}",
    },
}

# ---------------------------------------------------------------------------
# Fix #2 — robust language detection
# ---------------------------------------------------------------------------
_UZ_WORDS = set("""
qanday qaysi nechta qancha haqida boyicha kim bormi bormi jami narxi narx
bitim bitimlar bitimlarini sotuvchi xaridor savdo aylanma aylanmasi viloyat
viloyatida shartnoma korxona tovar mahsulot eng katta kichik yirik yil yilda
oy oyida nechchi nechta so'm som sum uzs dollar valyuta eksport import bekor
qilingan holat status forvard spot fyuchers bug'doy bugdoy sement un guruch
shakar tuz paxta benzin dizel g'isht gisht polat po'lat alyuminiy mis
o'rtacha ortacha top qaysi nechchi marta nechta nechta ta bor yo'q yoq
ko'rsat korsat chiqar ber ko'p kop kam eng oxirgi birinchi ikkinchi
""".split())
_UZ_APOS = re.compile(r"[ogOG][ʻʼ‘’'`]")
_CYR = re.compile(r"[а-яё]")
_TOKEN = re.compile(r"[a-zA-Z\u0400-\u04FF'’‘ʻʼ`]+")


def detect_language(text: str, default: str = "uz") -> str:
    """Return 'uz' or 'ru'.

    * Cyrillic words vote for Russian.
    * Uzbek keywords / o'/g' apostrophes vote for Uzbek.
    * Ambiguous short queries (e.g. 'BK001') fall back to `default` — which is
      'uz' in the exchange context (audit fix #2: never default to 'ru').
    """
    t = text.lower()
    tokens = _TOKEN.findall(t)
    ru_score = sum(1 for tok in tokens if _CYR.search(tok))
    uz_score = sum(1 for tok in tokens
                   if db.normalize_apostrophes(tok) in _UZ_WORDS)
    if _UZ_APOS.search(text):
        uz_score += 2
    if ru_score == 0 and uz_score == 0:
        return default
    return "ru" if ru_score > uz_score else "uz"


# ---------------------------------------------------------------------------
# Off-topic / greeting guard (fix #3)
# ---------------------------------------------------------------------------
_GREETINGS = re.compile(
    r"^\s*(salom|assalomu|alaykum|hello|hi|hey|привет|здравств|добрый\s+(день|вечер)|"
    r"доброе\s+утро|salomlar)\b", re.IGNORECASE)
_GREET_TAIL = re.compile(r"^[\s!.,?]*(bot|бот|uzex|ai|aka|opa|do'st|друг)?[\s!.,?]*$", re.IGNORECASE)

_DOMAIN_WORDS = set("""
bitim savdo aylanma narx narxi sotuvchi xaridor kompaniya korxona broker
shartnoma tovar mahsulot viloyat hudud yil oy jami ortacha o'rtacha eng top
eksport import valyuta dollar som so'm sum uzs usd bekor status holat forvard
spot fyuchers stir inn kod raqam hisobot statistika tahlil reyestr birja
сделка сделок сделки биржа оборот сумма цена цены продавец покупатель компания
брокер контракт договор товар продукт регион область город год месяц всего
средний экспорт импорт валюта доллар сум отмен аннулир форвард спот фьючерс
топ рейтинг крупнейш статистика анализ реестр инн код номер сколько какие
""".split())
_DOMAIN_WORDS |= {db.normalize_apostrophes(k) for k in db.PRODUCT_SYNONYMS}
_DOMAIN_WORDS |= {db.normalize_apostrophes(k) for k in db.REGION_MAP}


def is_greeting(text: str) -> bool:
    m = _GREETINGS.match(text.strip())
    if not m:
        return False
    rest = text.strip()[m.end():]
    # "salom" / "salom bot" -> greeting; "salom, 2024-yilda..." -> real question
    return bool(_GREET_TAIL.match(rest)) or (not re.search(r"\d", rest) and not rest.strip())


def is_off_topic(text: str, entities: dict) -> bool:
    """True when the text has no exchange meaning at all (gibberish guard)."""
    if any(entities.get(k) for k in
           ("participants", "products", "brokers", "contracts", "deals", "inns", "regions")):
        return False
    if re.search(r"\d", text):  # years, numbers, codes -> let the agent try
        return False
    tokens = [db.normalize_apostrophes(t) for t in _TOKEN.findall(text.lower())]
    if not tokens:
        return True
    return not any(t in _DOMAIN_WORDS for t in tokens)


# ---------------------------------------------------------------------------
# Fix #1 — Gemini client with retry + exponential backoff + model fallback
# ---------------------------------------------------------------------------
class LLMUnavailable(Exception):
    pass


_RETRYABLE = re.compile(r"429|503|500|502|504|UNAVAILABLE|RESOURCE_EXHAUSTED|"
                        r"overloaded|deadline|timeout|temporarily", re.IGNORECASE)


class LLM:
    def __init__(self, api_key: str | None = None, models: list[str] | None = None):
        self.api_key = (api_key or GEMINI_API_KEY).strip()
        self.models = models or GEMINI_MODELS
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def generate(self, prompt: str, temperature: float = 0.1,
                 max_tokens: int = 1200, retries: int = 3) -> str:
        if not self.available:
            raise LLMUnavailable("GEMINI_API_KEY is not configured")
        from google.genai import types
        client = self._get_client()
        last_err: Exception | None = None
        for attempt in range(retries):
            for model in self.models:
                try:
                    res = client.models.generate_content(
                        model=model, contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=temperature, max_output_tokens=max_tokens))
                    if res and res.text:
                        return res.text.strip()
                    last_err = LLMUnavailable("empty response")
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    msg = str(e)
                    if not _RETRYABLE.search(msg):
                        # invalid key / bad request -> do not hammer the API
                        raise LLMUnavailable(f"Gemini API error: {msg}") from e
                    log.warning("Gemini %s attempt %d: %s", model, attempt + 1, msg)
            # exponential backoff with jitter before the next round
            time.sleep(min(8.0, 1.0 * (2 ** attempt)) + random.uniform(0, 0.5))
        raise LLMUnavailable(f"Gemini API unavailable after retries: {last_err}")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def _sql_prompt(question: str, lang: str, entities_ctx: str, meta: dict) -> str:
    return f"""You are a senior SQLite analyst for the Uzbekistan Commodity Exchange (UZEX).
Convert the user's question into ONE safe SQLite SELECT query.

DATABASE SCHEMA:
{db.SCHEMA_TEXT}

DATA COVERAGE: {meta.get('min_date')} .. {meta.get('max_date')}, {meta.get('deal_count')} deals.
Entities found in the question:
{entities_ctx}

HARD RULES:
1. Output ONLY the SQL query (no markdown, no explanation). If the question is
   unrelated to UZEX exchange data, greetings, gibberish, or asks to modify data,
   output exactly: NO_QUERY
2. ONE single SELECT statement. Never INSERT/UPDATE/DELETE/DROP/PRAGMA/ATTACH.
3. Use amount_uzs for money totals (already converted to UZS). Use amount only
   when the user explicitly asks about the contract currency / USD.
4. For turnover / sums / averages / TOP rankings exclude cancelled deals:
   status != '{db.CANCELLED}'. For "how many deals exist in the database" count all.
5. Product names are stored in Russian. Use the EXACT product_name values from
   the entity context with LOWER(product_name) LIKE '%...%'. For Uzbek product
   words use their Russian equivalents (sement->цемент, bug'doy->пшеница, un->мука...).
6. Region values in deals are Uzbek latin ('Toshkent shahri', 'Navoiy viloyati'...).
7. Dates are ISO 'YYYY-MM-DD'. Filter years with deal_date LIKE '2024%' or
   strftime('%Y', deal_date)='2024'. Months with deal_date LIKE '2024-03%'.
8. Join participants p ON d.seller_inn=p.inn (or buyer_inn) for company names;
   brokers b ON d.seller_broker=b.broker_code (or buyer_broker) for broker names.
9. Add LIMIT 50 to list queries. Never use SELECT * for wide tables in lists.
10. If the question asks about a year outside {meta.get('min_date','2022')[:4]}–{meta.get('max_date','2026')[:4]},
    still write the query (it will return 0 and the system will say "no data").

User question ({'Uzbek' if lang == 'uz' else 'Russian'}): {question}
SQL:"""


def _relax_prompt(question: str, sql: str, lang: str, entities_ctx: str) -> str:
    return f"""The SQLite query for the user's question returned ZERO rows:
{sql}

User question: {question}
Entities: {entities_ctx}

Write ONE broader SQLite SELECT that keeps the main subject (product / company /
broker / metric) but relaxes the MOST restrictive filter (usually the year,
month, region or exact status) so we can offer the user CLOSE alternative
results. Keep it safe: single SELECT, no data modification, LIMIT 20.
If nothing reasonable can be relaxed, output exactly: NO_QUERY
Output ONLY the SQL (or NO_QUERY).
SQL:"""


def _answer_prompt(question: str, lang: str, sql: str, result: dict,
                   near_match: bool, relaxed_what: str, meta: dict) -> str:
    lang_name = "Uzbek (latin script)" if lang == "uz" else "Russian"
    near_rule = ""
    if near_match:
        prefix = STRINGS[lang]["near_prefix"]
        near_rule = f"""
IMPORTANT: The EXACT conditions of the question matched NOTHING. The rows below
come from a BROADENED query ({relaxed_what}). You MUST start your answer with
exactly this sentence, then present the close results:
"{prefix}"
Never present these rows as an exact answer to the original question."""
    return f"""You are the UZEX exchange data analyst. Answer the user's question
using ONLY the query result below. Never invent numbers, names or facts.

Question: {question}
SQL executed: {sql}
Result columns: {result['columns']}
Result rows (JSON): {result['rows'][:40]}
{near_rule}

RULES:
* Answer ONLY in {lang_name}. No other language, no Spanish/English.
* Be concise (max ~150 words). Format big numbers with spaces (42 724 135 079 484).
* Money is in UZS (so'm / сум) unless the column clearly says USD.
* If the result is empty or all zeros/NULL: say clearly that no such data exists
  in the database, and mention the data coverage {meta.get('min_date')} .. {meta.get('max_date')}.
* Do NOT output SQL (the system appends it separately).
* Reference that the answer is based on the UZEX deals registry.

Answer:"""


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------
def _is_empty_result(result: dict) -> bool:
    rows = result.get("rows", [])
    if not rows:
        return True
    # single aggregate row of 0 / None also counts as "no data"
    if len(rows) == 1:
        vals = [v for v in rows[0] if v is not None]
        if not vals:
            return True
        if all(isinstance(v, (int, float)) and v == 0 for v in vals):
            return True
    return False


def _fmt_num(v) -> str:
    if isinstance(v, float):
        if v == int(v):
            v = int(v)
        else:
            return f"{v:,.2f}".replace(",", " ")
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    return str(v)


def _result_preview(result: dict, max_rows: int = 12) -> str:
    cols = result["columns"]
    lines = []
    for row in result["rows"][:max_rows]:
        parts = []
        for c, v in zip(cols, row):
            if v is None:
                continue
            parts.append(f"{c}: {_fmt_num(v)}")
        lines.append(" • " + "; ".join(parts))
    if result.get("truncated") or len(result["rows"]) > max_rows:
        lines.append(" …")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------
@dataclass
class Answer:
    text: str
    sql: str | None = None
    near_sql: str | None = None
    near_match: bool = False
    lang: str = "uz"
    engine: str = "gemini"          # gemini | local
    rows: list = field(default_factory=list)
    elapsed: float = 0.0
    off_topic: bool = False


class UzexAgent:
    def __init__(self, db_path: str | None = None, llm: LLM | None = None):
        self.db_path = db_path or db.DB_PATH
        self.llm = llm or LLM()
        self.fallback = FallbackEngine(self.db_path)
        self.meta = db.get_meta(self.db_path)

    # -- public API ---------------------------------------------------------
    def answer(self, question: str, lang: str | None = None,
               default_lang: str = "uz") -> Answer:
        t0 = time.monotonic()
        question = (question or "").strip()
        lang = lang or detect_language(question, default=default_lang)
        S = STRINGS[lang]

        if not question:
            return Answer(text=S["off_topic"], lang=lang, engine="local", off_topic=True)
        if is_greeting(question):
            return Answer(text=S["greeting"], lang=lang, engine="local", off_topic=True)

        entities = db.search_entities(question, self.db_path)
        if is_off_topic(question, entities):
            return Answer(text=S["off_topic"], lang=lang, engine="local", off_topic=True)

        ent_ctx = db.entities_context(entities)

        # 1) obtain SQL -----------------------------------------------------
        sql: str | None = None
        engine = "gemini"
        llm_failed = False
        if self.llm.available:
            try:
                raw = self.llm.generate(_sql_prompt(question, lang, ent_ctx, self.meta))
                sql = self._extract_sql(raw)
            except LLMUnavailable as e:
                log.warning("LLM unavailable for SQL: %s", e)
                llm_failed = True
        if sql is None:
            if self.llm.available and not llm_failed:
                # LLM genuinely said NO_QUERY -> respect it (off-topic guard, fix #3)
                return Answer(text=S["off_topic"], lang=lang, engine="local", off_topic=True)
            # No API key, or the API is down -> deterministic local engine (fix #1)
            engine = "local"

        # 2) execute with self-correction ------------------------------------
        result = None
        if engine == "gemini":
            result, sql = self._execute_with_correction(question, lang, sql, ent_ctx)
            if result is None:
                engine = "local"  # Gemini SQL kept failing -> local engine
        if engine == "local":
            plan = self.fallback.build(question, lang)
            if plan is None:
                return Answer(text=S["off_topic"], lang=lang, engine="local", off_topic=True)
            sql = plan.sql
            try:
                result = db.run_sql(sql, db_path=self.db_path)
            except Exception as e:  # noqa: BLE001
                log.error("local SQL failed: %s", e)
                return Answer(text=S["no_data"] + "\n" + S["coverage"].format(
                    min_d=self.meta.get("min_date"), max_d=self.meta.get("max_date"),
                    n=_fmt_num(int(self.meta.get("deal_count", 0)))), lang=lang, engine="local")

        # 3) near-match relaxation -------------------------------------------
        near_match = False
        near_sql = None
        relaxed_what = ""
        original_sql = sql
        if _is_empty_result(result):
            relaxed = self._relax(question, lang, sql, ent_ctx, engine)
            if relaxed:
                near_sql, relaxed_what, near_result = relaxed
                if not _is_empty_result(near_result):
                    result, near_match = near_result, True
                    # keep the ORIGINAL sql for transparency; near_sql holds
                    # the broadened query that produced the close results

        # 4) compose the answer ----------------------------------------------
        if _is_empty_result(result):
            text = S["no_data"] + "\n\n" + S["coverage"].format(
                min_d=self.meta.get("min_date"), max_d=self.meta.get("max_date"),
                n=_fmt_num(int(self.meta.get("deal_count", 0))))
            return Answer(text=text, sql=sql, lang=lang, engine=engine,
                          elapsed=time.monotonic() - t0)

        text = None
        if engine == "gemini" and self.llm.available:
            effective_sql = near_sql if near_match else sql
            try:
                text = self.llm.generate(_answer_prompt(
                    question, lang, effective_sql, result, near_match, relaxed_what, self.meta),
                    temperature=0.2, max_tokens=900)
            except LLMUnavailable as e:
                log.warning("LLM unavailable for answer: %s", e)
        if not text:
            text = self._local_answer(question, lang, result, near_match, relaxed_what)
            if engine == "gemini":
                text = S["api_down"] + "\n\n" + text

        return Answer(text=text, sql=original_sql, near_sql=near_sql, near_match=near_match,
                      lang=lang, engine=engine, rows=result["rows"][:40],
                      elapsed=time.monotonic() - t0)

    # -- internals -----------------------------------------------------------
    @staticmethod
    def _extract_sql(raw: str) -> str | None:
        raw = raw.strip()
        if "NO_QUERY" in raw.upper():
            return None
        m = re.search(r"```(?:sql)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
        raw = raw.strip().rstrip(";").strip()
        if re.match(r"^(SELECT|WITH)\b", raw, re.IGNORECASE):
            return raw
        return None

    def _execute_with_correction(self, question, lang, sql, ent_ctx, attempts: int = 2):
        last_sql = sql
        for i in range(attempts + 1):
            ok, reason = db.validate_sql(last_sql)
            if not ok:
                err = reason
            else:
                try:
                    return db.run_sql(last_sql, db_path=self.db_path), last_sql
                except Exception as e:  # noqa: BLE001
                    err = str(e)
            if i >= attempts or not self.llm.available:
                break
            try:
                fix_prompt = (f"This SQLite query failed:\n{last_sql}\nError: {err}\n"
                              f"Schema:\n{db.SCHEMA_TEXT}\nEntities:\n{ent_ctx}\n"
                              f"Question: {question}\n"
                              "Return ONLY the corrected single SELECT query (or NO_QUERY):")
                raw = self.llm.generate(fix_prompt)
                fixed = self._extract_sql(raw)
                if not fixed:
                    break
                last_sql = fixed
            except LLMUnavailable:
                break
        return None, last_sql

    def _relax(self, question, lang, sql, ent_ctx, engine):
        """Return (relaxed_sql, what_changed, result) or None."""
        # deterministic relaxation first (fast, no API)
        relaxed_sql, what = self.fallback.relax_sql(sql, lang)
        if relaxed_sql:
            try:
                res = db.run_sql(relaxed_sql, db_path=self.db_path)
                if not _is_empty_result(res):
                    return relaxed_sql, what, res
            except Exception:  # noqa: BLE001
                pass
        # LLM relaxation
        if engine == "gemini" and self.llm.available:
            try:
                raw = self.llm.generate(_relax_prompt(question, sql, lang, ent_ctx))
                rsql = self._extract_sql(raw)
                if rsql:
                    ok, _ = db.validate_sql(rsql)
                    if ok:
                        res = db.run_sql(rsql, db_path=self.db_path)
                        if not _is_empty_result(res):
                            what = ("yil/hudud/status filtri kengaytirildi" if lang == "uz"
                                    else "фильтр года/региона/статуса расширен")
                            return rsql, what, res
            except (LLMUnavailable, Exception):  # noqa: BLE001
                pass
        return None

    def _local_answer(self, question, lang, result, near_match, relaxed_what) -> str:
        S = STRINGS[lang]
        body = self.fallback.describe(question, lang, result, relaxed=near_match)
        if not body:
            body = _result_preview(result)
        if body:
            body = body[0].upper() + body[1:]
        if near_match:
            note = S["relaxed_note"].format(what=relaxed_what) if relaxed_what else ""
            return S["near_prefix"] + ("\n" + note if note else "") + "\n\n" + body
        return body


def format_footer(answer: Answer, include_sql: bool = True) -> str:
    """Append source + SQL transparency block (judges' prototype-quality criterion)."""
    S = STRINGS[answer.lang]
    parts = [answer.text]
    if include_sql and answer.sql:
        parts.append(f"\n{S['sql_label']}:\n{answer.sql}")
    return "\n".join(parts)
