# -*- coding: utf-8 -*-
"""Unicode search helpers.

SQLite COLLATE NOCASE does not fold Cyrillic, and company/region names use
the Unicode apostrophe ‘ (U+2018), not ASCII '. Telegram Markdown also eats
underscores, so answers are rendered as HTML <pre>.
"""
import html
import re

# Apostrophe and quote variants seen in the UZEX files.
_STRIP = "'\u2018\u2019\u201b\u02bc\u02bb\u2032\u0060\u00b4\"«»“”"


def ufold(value):
    """Case-fold and strip apostrophes/quotes so LIKE matches dirty names."""
    if value is None:
        return None
    s = str(value).casefold()
    for ch in _STRIP:
        s = s.replace(ch, "")
    return s


_LIKE = re.compile(
    r"(?P<col>(?:[A-Za-z_][\w]*\.)?[A-Za-z_][\w]*)\s+(?P<not>NOT\s+)?LIKE\s+(?P<pat>'(?:[^']|'')*')(?:\s+COLLATE\s+\w+)?",
    re.IGNORECASE,
)

_COTTON_STRONG = (
    "(ufold(product_name) LIKE ufold('%хлопковое масло%') "
    "OR ufold(product_name) LIKE ufold('%масло хлопковое%'))"
)


def is_cotton_oil_question(question: str) -> bool:
    q = ufold(question or "")
    if "paxta" in q and "yog" in q:
        return True
    if "пахта" in q and ("ёг" in q or "йог" in q or "масл" in q):
        return True
    if "хлопковое масло" in q or "масло хлопковое" in q:
        return True
    return False


def _cotton_like(pattern_sql: str) -> bool:
    raw = pattern_sql.replace("''", "").casefold()
    return any(tok in raw for tok in ("хлопков", "пахта", "йог", "ёг", "хлопковое"))


def strengthen_product_likes(sql: str, question: str = "") -> str:
    """Replace weak cotton-oil LIKE patterns with both Russian word orders."""
    cotton_q = is_cotton_oil_question(question)
    if not cotton_q and not _cotton_like(sql):
        return sql

    def repl(m):
        if m.group("not"):
            return m.group(0)
        pat = m.group("pat")
        col = m.group("col")
        if "product_name" not in col.lower():
            return m.group(0)
        if cotton_q or _cotton_like(pat):
            return _COTTON_STRONG
        return m.group(0)

    return _LIKE.sub(repl, sql)


def rewrite_like(sql: str) -> str:
    """Make remaining LIKE comparisons Unicode-aware via ufold()."""

    def repl(m):
        # Already rewritten cotton predicate contains ufold( — leave it.
        if m.group(0).lower().startswith("ufold"):
            return m.group(0)
        not_kw = "NOT " if m.group("not") else ""
        col = m.group("col")
        pat = m.group("pat")
        return f"ufold({col}) {not_kw}LIKE ufold({pat})"

    return _LIKE.sub(repl, sql)


def tighten_executed_only(sql: str, question: str) -> str:
    """'faqat ijro etilgan' must not include partial executions."""
    q = ufold(question or "")
    strict = ("faqat ijro" in q) or ("tolko ispol" in q) or ("только исполнен" in (question or "").lower())
    if not strict:
        return sql
    sql = sql.replace(", 'Исполнен(Частично)'", "")
    sql = sql.replace("'Исполнен(Частично)', ", "")
    return sql


def prepare_sql(sql: str, question: str = "") -> str:
    if not sql:
        return sql
    sql = strengthen_product_likes(sql, question)
    sql = tighten_executed_only(sql, question)
    sql = rewrite_like(sql)
    return sql


def search_hints(question: str) -> str:
    """Extra rules appended to the Gemini prompt for this question."""
    lines = [
        "QIDIRUV QOIDALARI / ПРАВИЛА ПОИСКА:",
        "- SQLite COLLATE NOCASE кириллни кичик ҳарф қилмайди. Маҳсулот номи русча ва бош ҳарфда: 'Хлопковое масло', 'Масло хлопковое'.",
        "- Apostrof bazada oddiy ' emas, Unicode ‘ (U+2018): Farg‘ona, Mo‘jotat. LIKE '%Mo''jotat%' ishlamasligi mumkin. '%Mo%jotat%' yoki '%Farg%ona%' ishlating.",
        "- Bir bitimni ikki marta sanamang. Broker uchun: WHERE seller_broker=X OR buyer_broker=X (UNION ALL emas).",
        "- savdo/sotgan/продажи = faqat seller_inn. xarid/olgan/покуп = faqat buyer_inn.",
        "- 'faqat ijro etilgan' = deal_status = 'Исполнен' aniq. 'Исполнен(Частично)' va 'Аннулирован' ni qo'shmang, agar foydalanuvchi bekor qilinganini olmading desa.",
        "- Eksport summasini so'mda so'rasa, deal_sum_uzs (dollar * shu kundagi kurs) ishlating, yalang deal_sum emas.",
        "- 0 qator yoki COUNT=0 bo'lsa raqam to'qimang. 'yo'q' deb ayting.",
        "- '3-avgust 1$lik shartnoma' — salom emas. Sana + summa + valyuta. "
        "1$ = deal_sum yoki base_price = 1 va currency = 'Доллар США'. "
        "Aniq mos bo'lmasa 0 qator, dollar shartnoma to'qilmaydi. 'shartoma' = shartnoma.",
    ]
    if is_cotton_oil_question(question):
        lines.append(
            "- Bu savol PAXTA YOG'I haqida. product_name: 'Хлопковое масло' YOKI 'Масло хлопковое'. "
            "Kungaboqar (подсолнечн) va shrotni QO'SHMANG. '%пахта%йог%' va '%хлопков%' yetarli emas — "
            "ikki so'z tartibini ham qidiring."
        )
    ql = (question or "").lower()
    if "toshkent shah" in ufold(question or "") or "toshkent shah" in ql:
        lines.append("- Toshkent shahri va Toshkent viloyati BOSHQA-BOSHQA. Shahar so'ralsa viloyatni chiqarib tashlang.")
    return "\n".join(lines)


def as_telegram_html(text: str) -> str:
    """Render the answer as Telegram HTML so '_' in SQL is not eaten."""
    cleaned = text.replace("**", "").replace("```sql", "").replace("```", "")
    # quote=False: Telegram HTML does not reliably decode &#x27;, and SQL is full of quotes.
    return "<pre>" + html.escape(cleaned, quote=False) + "</pre>"
