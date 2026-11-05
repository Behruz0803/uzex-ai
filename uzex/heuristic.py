# -*- coding: utf-8 -*-
"""
Heuristic Text-to-SQL fallback when Gemini is not available (no API key or no internet).
Covers 80% of hackathon questions via regex patterns.
"""
import re

def detect_lang(text):
    has_cyr = bool(re.search(r"[а-яё]", text.lower()))
    # Uzbek markers
    uz_words = ["qancha", "qaysi", "broker", "bitim", "yilda", "sotuvchi", "xaridor", "narx", "eng", "ko'p", "kam"]
    ru_words = ["сколько", "какой", "брокер", "сделк", "году", "продавец", "покупатель", "цена", "самый"]
    uz_score = sum(1 for w in uz_words if w in text.lower())
    ru_score = sum(1 for w in ru_words if w in text.lower())
    if has_cyr and ru_score >= uz_score:
        return "ru"
    if uz_score > ru_score:
        return "uz"
    return "ru" if has_cyr else "uz"

def extract_inn(text):
    # INN is 9 digits
    m = re.search(r"\b\d{9}\b", text)
    return m.group(0) if m else None

def extract_broker(text):
    m = re.search(r"\bBK\d{3}\b", text, re.IGNORECASE)
    return m.group(0).upper() if m else None

def extract_year(text):
    # Match any 4-digit year (incl. out-of-coverage 2018/2030) so the query
    # filters that year, returns 0 rows, and the near-match relaxation can
    # offer close results instead of silently showing all years.
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return m.group(1) if m else None


_MONTHS = (
    ("yanvar", "январ", 1),
    ("fevral", "феврал", 2),
    ("mart", "март", 3),
    ("aprel", "апрел", 4),
    ("may", "май", 5),
    ("iyun", "июн", 6),
    ("iyul", "июл", 7),
    ("avgust", "август", 8),
    ("sentabr", "сентябр", 9),
    ("oktabr", "октябр", 10),
    ("noyabr", "ноябр", 11),
    ("dekabr", "декабр", 12),
)


def extract_month(text):
    low = text.lower()
    for uz, ru, num in _MONTHS:
        if re.search(rf"\b{uz}\w*", low) or re.search(rf"\b{ru}\w*", low):
            return num
    m = re.search(r"\b(0?[1-9]|1[0-2])\s*[-/.]?\s*oy", low)
    return int(m.group(1)) if m else None


def _period(year, month):
    if year and month:
        import calendar
        last = calendar.monthrange(int(year), int(month))[1]
        start = f"{year}-{int(month):02d}-01"
        end = f"{year}-{int(month):02d}-{last:02d}"
        return f"deal_date BETWEEN '{start}' AND '{end}'", f"{year}-{int(month):02d}"
    if year:
        return f"deal_date BETWEEN '{year}-01-01' AND '{year}-12-31'", str(year)
    return "1=1", "barcha yillar"


def _done(sql, exp, lang):
    return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True, "confident": True}


def extract_calendar(text):
    """Yil, oy, kun. Kun faqat oy nomi yoki to'liq sana yonida bo'lsa."""
    low = text.lower().replace("’", "'").replace("‘", "'")
    m = re.search(r"\b(202[2-6])-(\d{2})-(\d{2})\b", text)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](202[2-6])\b", text)
    if m:
        return int(m.group(3)), int(m.group(2)), int(m.group(1))
    year_s = extract_year(text)
    month = extract_month(text)
    day = None
    if month:
        for uz, ru, num in _MONTHS:
            if num != month:
                continue
            m = re.search(
                rf"\b(\d{{1,2}})(?:\s*[-]?(?:го|chi|i))?\s*[-.]?\s*(?:{uz}|{ru})\w*",
                low,
            )
            if not m:
                m = re.search(rf"\b(?:{uz}|{ru})\w*\s+(\d{{1,2}})\b", low)
            if m:
                day = int(m.group(1))
                if 1 <= day <= 31:
                    break
                day = None
    year = int(year_s) if year_s else None
    return year, month, day


def _date_pred(col, year, month, day):
    import calendar
    if year and month and day:
        if not (1 <= int(month) <= 12 and 1 <= int(day) <= calendar.monthrange(int(year), int(month))[1]):
            return None, None
        iso = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return f"{col} = '{iso}'", iso
    if year and month:
        last = calendar.monthrange(int(year), int(month))[1]
        start = f"{int(year):04d}-{int(month):02d}-01"
        end = f"{int(year):04d}-{int(month):02d}-{last:02d}"
        return f"{col} BETWEEN '{start}' AND '{end}'", f"{start}..{end}"
    if month and day and not year:
        if not (1 <= int(day) <= 31 and 1 <= int(month) <= 12):
            return None, None
        md = f"{int(month):02d}-{int(day):02d}"
        return f"strftime('%m-%d', {col}) = '{md}'", md
    if year:
        return f"{col} BETWEEN '{int(year)}-01-01' AND '{int(year)}-12-31'", str(year)
    return "1=1", "barcha sanalar"


def _amt(amount):
    return f"{float(amount):.4f}".rstrip("0").rstrip(".")


def _money_amount(text):
    """Faqat valyuta yozuvi yonidagi son. Yil va kunni summa deb olmaydi."""
    ql = text.lower().replace("’", "'").replace("‘", "'").replace("`", "'")
    usd = re.search(
        r"(\d+(?:[.,]\d+)?)(?:\s*(ming|тысяч\w*))?\s*[-]?\s*(?:\$|usd|dollar\w*|доллар\w*)"
        r"|\$\s*(\d+(?:[.,]\d+)?)",
        ql,
    )
    if not usd and re.search(r"\b(bir|один|одна)\s*[-]?\s*(?:\$|dollar\w*|доллар\w*|usd)", ql):
        return 1.0, "USD"
    if usd:
        raw = usd.group(1) or usd.group(3)
        val = float(raw.replace(",", "."))
        if usd.group(2):
            val *= 1000.0
        return val, "USD"
    uzs = re.search(
        r"(\d+(?:[.,]\d+)?)(?:\s*(ming|тысяч\w*))?\s*[-]?\s*(?:so'm\w*|somlik|som\w*|сум\w*|uzs\w*)",
        ql,
    )
    if not uzs and re.search(r"\b(bir)\s*[-]?\s*(?:so'm|som|сум)", ql):
        return 1.0, "UZS"
    if uzs:
        val = float(uzs.group(1).replace(",", "."))
        if uzs.group(2):
            val *= 1000.0
        return val, "UZS"
    return None, None


def looks_like_market(question: str) -> bool:
    ql = (question or "").lower().replace("’", "'").replace("‘", "'")
    if any(w in ql for w in (
        "$", "dollar", "доллар", "usd", "shartn", "shartom", "bitim", "сдел",
        "kontrakt", "контракт", "broker", "брокер", "stir", "инн", "so'm", "сум",
        "kurs", "курс", "qimmat", "arzon", "aksiya", "акция",
    )):
        return True
    year, month, day = extract_calendar(question or "")
    return bool(year and (month or day))


def dated_money_sql(question: str):
    """Sana + dollar/so'm summa. Salom emas, aniq qidiruv."""
    lang = detect_lang(question)
    ql = question.lower().replace("’", "'").replace("‘", "'").replace("`", "'")
    amount, money = _money_amount(question)
    year, month, day = extract_calendar(question)
    ranking = any(w in ql for w in (
        "qimmat", "arzon", "top", "топ", "eng ko", "дорог", "дешев", "broker", "брокер",
    ))
    # Reyting savolini (eng qimmat bitim) bu naqsh yutib yubormasin.
    if ranking and amount is None:
        return None
    wants_contract = any(w in ql for w in ("shartn", "shartom", "kontrakt", "контракт"))
    wants_deal = any(w in ql for w in ("bitim", "сдел", "shartn", "shartom", "kontrakt", "контракт"))
    wants_rate = any(w in ql for w in ("kurs", "курс")) and money == "USD"
    deal_pred, deal_label = _date_pred("d.deal_date", year, month, day)
    reg_pred, reg_label = _date_pred("reg_date", year, month, day)
    rate_pred, rate_label = _date_pred("rate_date", year, month, day)
    if (year and month and day) and (deal_pred is None or reg_pred is None):
        sql = "SELECT 'noto''g''ri sana' AS xato WHERE 0;"
        exp = "Sana kalendarda yo'q. Qidiruv qilinmadi, raqam to'qilmaydi." if lang == "uz" else "Такой даты нет в календаре."
        return _done(sql, exp, lang)

    if wants_rate and rate_pred:
        mult = amount if amount is not None else 1.0
        sql = (
            "SELECT rate_date, rate AS som_per_usd, "
            f"ROUND(rate * {mult}, 2) AS som "
            f"FROM usd_rates WHERE {rate_pred} ORDER BY rate_date LIMIT 31;"
        )
        exp = (
            f"{_amt(mult)} AQSH dollari kursi, so'm ({rate_label}). Bu shartnoma narxi emas."
            if lang == "uz" else
            f"Курс {_amt(mult)} USD в сумах ({rate_label})."
        )
        return _done(sql, exp, lang)

    if amount is None:
        # To'liq kun: shu kundagi bitim/shartnoma. Oy-darajadagi reytingsiz savolni bu yerga tortmaymiz.
        if not (year and month and day):
            return None
        if wants_contract and "bitim" not in ql and "сдел" not in ql:
            sql = (
                "SELECT contract_id, reg_date, product_name, currency, "
                "ROUND(base_price, 2) AS base_price, unit, contract_type "
                f"FROM contracts WHERE {reg_pred} ORDER BY contract_id LIMIT 20;"
            )
            exp = f"{reg_label} da ro'yxatdan o'tgan shartnomalar. Summa so'ralmagan." if lang == "uz" else f"Контракты, зарегистрированные {reg_label}."
            return _done(sql, exp, lang)
        sql = (
            "SELECT IFNULL(c.currency, '?') AS currency, COUNT(*) AS bitim, "
            "ROUND(MIN(d.deal_sum), 2) AS min_sum, ROUND(MAX(d.deal_sum), 2) AS max_sum "
            "FROM deals d LEFT JOIN contracts c ON c.contract_id = d.contract_id "
            f"WHERE {deal_pred} GROUP BY c.currency ORDER BY bitim DESC;"
        )
        exp = f"{deal_label} dagi bitimlar, valyuta bo'yicha. Aniq summa so'ralmagan." if lang == "uz" else f"Сделки за {deal_label} по валюте."
        return _done(sql, exp, lang)

    if not wants_deal and not wants_contract and money is None:
        return None
    # "1$lik shartoma" — shartnoma so'zi bo'lmasa ham dollar summasi birja savoli.
    currency = "Доллар США" if money == "USD" else "Узбекский сум"
    unit_name = "USD" if money == "USD" else "so'm"
    tol = 0.01 if amount >= 1 else 0.0001
    product = extract_product(question)
    prod_d = f" AND ufold(d.product_name) LIKE ufold('%{product}%')" if product else ""
    prod_c = f" AND ufold(product_name) LIKE ufold('%{product}%')" if product else ""
    label = deal_label or reg_label
    sql = f"""
WITH exact AS (
  SELECT 'aniq shartnoma' AS manba, CAST(contract_id AS TEXT) AS id, reg_date AS sana,
         product_name AS mahsulot, ROUND(base_price, 2) AS summa, currency AS valyuta,
         unit AS birlik, contract_type AS holat, 'base_price' AS mezon,
         ROUND(ABS(base_price - {amount}), 2) AS farq
  FROM contracts
  WHERE currency = '{currency}' AND ABS(base_price - {amount}) < {tol} AND {reg_pred}{prod_c}
  UNION ALL
  SELECT 'aniq bitim summasi', CAST(d.deal_id AS TEXT), d.deal_date, d.product_name,
         ROUND(d.deal_sum, 2), c.currency, d.unit, d.deal_status, 'deal_sum',
         ROUND(ABS(d.deal_sum - {amount}), 2)
  FROM deals d
  JOIN contracts c ON c.contract_id = d.contract_id
  WHERE c.currency = '{currency}' AND ABS(d.deal_sum - {amount}) < {tol} AND {deal_pred}{prod_d}
  UNION ALL
  SELECT 'aniq birlik narxi', CAST(d.deal_id AS TEXT), d.deal_date, d.product_name,
         ROUND(d.deal_sum * 1.0 / (d.quantity * d.lot), 2), c.currency, d.unit, d.deal_status, 'unit_price',
         ROUND(ABS(d.deal_sum * 1.0 / (d.quantity * d.lot) - {amount}), 2)
  FROM deals d
  JOIN contracts c ON c.contract_id = d.contract_id
  WHERE c.currency = '{currency}' AND d.quantity > 0 AND d.lot > 0
    AND ABS(d.deal_sum * 1.0 / (d.quantity * d.lot) - {amount}) < {tol} AND {deal_pred}{prod_d}
)
SELECT * FROM exact
UNION ALL
SELECT * FROM (
  SELECT 'ANIQ EMAS — eng yaqin bitim' AS manba, CAST(d.deal_id AS TEXT), d.deal_date,
         d.product_name, ROUND(d.deal_sum, 2), c.currency, d.unit, d.deal_status,
         'eng yaqin deal_sum', ROUND(ABS(d.deal_sum - {amount}), 2)
  FROM deals d
  JOIN contracts c ON c.contract_id = d.contract_id
  WHERE c.currency = '{currency}' AND d.deal_sum > 0 AND {deal_pred}{prod_d}
    AND NOT EXISTS (SELECT 1 FROM exact)
  ORDER BY ABS(d.deal_sum - {amount})
  LIMIT 5
);
""".strip()
    exp = (
        f"Aniq {_amt(amount)} {unit_name} qidirildi ({label}): shartnoma bazaviy narxi, bitim summasi va birlik narxi. "
        f"Valyuta: {currency}. Aniq mos bo'lmasa, 'ANIQ EMAS' qatorlari shu filtrdagi eng yaqin bitim — ular {_amt(amount)} {unit_name} emas. Raqam to'qilmaydi."
        if lang == "uz" else
        f"Искалась точная сумма {_amt(amount)} {unit_name} ({label}). Строки «ANIQ EMAS» — ближайшие сделки, не точная сумма."
    )
    return _done(sql, exp, lang)


def casual_ranking_sql(question: str):
    """Qisqa og'zaki savollar: Gemini ularni salom deb o'qimasin."""
    lang = detect_lang(question)
    ql = question.lower().replace("’", "'").replace("`", "'")
    year = extract_year(question)
    month = extract_month(question)
    period_sql, period_label = _period(year, month)
    has_broker = "broker" in ql or "брокер" in ql
    has_deal = "bitim" in ql or "сдел" in ql
    expensive = any(w in ql for w in ("qimmat", "дорог", "дорого"))
    cheap = any(w in ql for w in ("arzon", "дешев", "дешёв"))
    share = any(w in ql for w in ("aksiya", "акция", "aksiy"))
    # Tovar nomi bor savolni (paxta yog'i, sement) bu umumiy naqsh yutib yubormasin.
    if not share and extract_product(question):
        return None
    turnover = any(w in ql for w in ("aylantir", "oborot", "оборот", "pul aylantir", "aylanma"))
    topish = any(w in ql for w in ("top", "топ", "eng ko", "ko'p", "ko‘p", "kop ", "больше всего"))

    if share:
        year_sql = f" AND {period_sql}" if year or month else ""
        sql = (
            "SELECT deal_id, deal_date, product_name, "
            "ROUND(unit_price_uzs, 2) AS unit_price_uzs, ROUND(deal_sum_uzs, 2) AS deal_sum_uzs "
            "FROM deals_enriched WHERE ("
            "ufold(product_name) LIKE ufold('%акция%') "
            "OR ufold(product_name) LIKE ufold('%акци%') "
            "OR ufold(product_name) LIKE ufold('%aksiya%')"
            f"){year_sql} LIMIT 20;"
        )
        exp = (
            "Aksiya/ulush/qimmatli qog'oz qidirildi. Bu baza tovar-xomashyo birjasi, "
            "aksiya bo'lmasa 0 qator — raqam to'qilmaydi."
            if lang == "uz" else
            "Поиск акций. Это товарно-сырьевая биржа; если акций нет, 0 строк — число не выдумывается."
        )
        return _done(sql, exp, lang)

    if has_broker and (turnover or (expensive and "pul" in ql)):
        sql = f"""
SELECT b.broker_code, b.name,
       ROUND(SUM(x.turnover), 2) AS turnover_uzs,
       SUM(x.deals) AS deals
FROM (
  SELECT seller_broker AS broker_code, SUM(deal_sum_uzs) AS turnover, COUNT(*) AS deals
  FROM deals_enriched
  WHERE deal_status != 'Аннулирован(Не оплачен)' AND {period_sql}
    AND seller_broker IS NOT NULL AND seller_broker != ''
  GROUP BY seller_broker
  UNION ALL
  SELECT buyer_broker, SUM(deal_sum_uzs), COUNT(*)
  FROM deals_enriched
  WHERE deal_status != 'Аннулирован(Не оплачен)' AND {period_sql}
    AND buyer_broker IS NOT NULL AND buyer_broker != ''
    AND buyer_broker != IFNULL(seller_broker, '')
  GROUP BY buyer_broker
) x
JOIN brokers b ON b.broker_code = x.broker_code
GROUP BY b.broker_code, b.name
ORDER BY turnover_uzs DESC
LIMIT 5;
""".strip()
        exp = (
            f"Brokerlar pul aylanmasi, so'mda, bekor qilinganlar yo'q ({period_label}). "
            "Bir bitimda broker ikki tomonda bo'lsa, bir marta sanaladi."
            if lang == "uz" else
            f"Оборот брокеров в сумах без аннулированных ({period_label})."
        )
        return _done(sql, exp, lang)

    if has_broker and (topish or "qaysi" in ql or "какой" in ql or "kim" in ql):
        sql = f"""
SELECT b.broker_code, b.name, COUNT(*) AS deals
FROM deals d
JOIN brokers b ON b.broker_code = d.seller_broker OR b.broker_code = d.buyer_broker
WHERE d.deal_status != 'Аннулирован(Не оплачен)' AND {period_sql}
GROUP BY b.broker_code, b.name
ORDER BY deals DESC
LIMIT 5;
""".strip()
        exp = (
            f"Top brokerlar bitim soni bo'yicha, bekor qilinganlar yo'q ({period_label}). "
            "Sotuvchi yoki xaridor tomoni, bitim ikki marta sanalmaydi."
            if lang == "uz" else
            f"Топ брокеров по числу сделок без аннулированных ({period_label})."
        )
        return _done(sql, exp, lang)

    if expensive and has_deal and "narx" not in ql and "цена" not in ql:
        sql = f"""
SELECT deal_id, deal_date, product_name, deal_status, currency, unit,
       ROUND(deal_sum_uzs, 2) AS deal_sum_uzs,
       ROUND(unit_price_uzs, 2) AS unit_price_uzs,
       seller_broker, buyer_broker
FROM deals_enriched
WHERE deal_status = 'Исполнен' AND deal_sum_uzs IS NOT NULL AND {period_sql}
ORDER BY deal_sum_uzs DESC
LIMIT 1;
""".strip()
        exp = (
            f"Eng qimmat ijro etilgan bitim — eng katta summa so'mda ({period_label}). "
            "Eksport dollari shu kundagi kursga ko'paytirilgan."
            if lang == "uz" else
            f"Самая дорогая исполненная сделка по сумме в сумах ({period_label})."
        )
        return _done(sql, exp, lang)

    if cheap and has_deal:
        sql = f"""
SELECT deal_id, deal_date, product_name, deal_status, currency, unit,
       ROUND(deal_sum_uzs, 2) AS deal_sum_uzs,
       ROUND(unit_price_uzs, 2) AS unit_price_uzs
FROM deals_enriched
WHERE deal_status = 'Исполнен' AND deal_sum_uzs > 0 AND {period_sql}
ORDER BY deal_sum_uzs ASC
LIMIT 1;
""".strip()
        exp = f"Eng arzon ijro etilgan bitim — eng kichik summa so'mda ({period_label})." if lang == "uz" else f"Самая дешёвая исполненная сделка по сумме в сумах ({period_label})."
        return _done(sql, exp, lang)

    if cheap or (expensive and not has_broker):
        order = "ASC" if cheap else "DESC"
        metric = "unit_price_uzs"
        if expensive and has_deal:
            metric = "deal_sum_uzs"
        label = "Eng arzon birlik narxi" if cheap else "Eng qimmat birlik narxi"
        sql = f"""
SELECT '{label}' AS mezon, deal_id, deal_date, product_name, unit, deal_status,
       ROUND(unit_price_uzs, 2) AS unit_price_uzs,
       ROUND(deal_sum_uzs, 2) AS deal_sum_uzs
FROM deals_enriched
WHERE deal_status = 'Исполнен' AND quantity > 0 AND lot > 0 AND unit_price_uzs > 0 AND {period_sql}
ORDER BY {metric} {order}
LIMIT 1;
""".strip()
        exp = (
            f"{label}, ijro etilgan bitimlar, so'm ({period_label}). "
            "Birlik narxi = bitim summasi / (soni × lot)."
            if lang == "uz" else
            f"{label}, исполненные сделки, в сумах ({period_label})."
        )
        return _done(sql, exp, lang)

    return None

def extract_product(text):
    # naive: known products list from DB
    products_keywords = {
        "sement": "цемент",
        "tsement": "цемент",
        "цемент": "цемент",
        "paxta": "хлопок",
        "хлопок": "хлопок",
        "yog": "масло",
        "масло": "масло",
        "benzin": "бензин",
        "бензин": "бензин",
        "karbamid": "карбамид",
        "полиэтилен": "полиэтилен",
        "полипропилен": "полипропилен",
        "shrot": "шрот",
        "шрот": "шрот",
        "soda": "сода",
        "qatron": "qatron",
        "mis": "медный",
        "мед": "медный",
        "truba": "труба",
        "quvur": "труба",
    }
    low = text.lower()
    for k, v in products_keywords.items():
        if k in low:
            return v
    return None

def heuristic_sql(question: str):
    """
    Returns dict with keys like generate_sql: sql, explanation, language, needs_data
    """
    dated = dated_money_sql(question)
    if dated:
        return dated

    casual = casual_ranking_sql(question)
    if casual:
        return casual

    lang = detect_lang(question)
    ql = question.lower()
    inn = extract_inn(question)
    broker = extract_broker(question)
    year = extract_year(question)
    product = extract_product(question)

    # Region detection (computed early so count/sum branches can filter by it)
    region_map = {
        "toshkent": "Toshkent", "ташкент": "Ташкент", "samarqand": "Samarqand",
        "самарканд": "Самарканд", "fergana": "Farg‘ona", "farg'ona": "Farg‘ona",
        "fargona": "Farg‘ona", "фергана": "Фергана", "namangan": "Namangan",
        "наманган": "Наманган", "buxoro": "Buxoro", "бухара": "Бухара",
        "andijon": "Andijon", "андижан": "Андижан", "navoiy": "Navoiy",
        "навои": "Навои", "jizzax": "Jizzax", "джизак": "Джизак",
        "qashqadaryo": "Qashqadaryo", "кашкадар": "Кашкадар",
        "qoraqalpog": "Qoraqalpog", "каракалпак": "Каракалпак",
        "sirdaryo": "Sirdaryo", "сырдар": "Сырдар", "surxondaryo": "Surxondaryo",
        "сурхандар": "Сурхандар", "xorazm": "Xorazm", "хорезм": "Хорезм",
    }
    region_found = None
    for k, v in region_map.items():
        if k in ql:
            region_found = v
            break

    # Helper date filter
    def date_filter(y):
        if y:
            return f"deal_date BETWEEN '{y}-01-01' AND '{y}-12-31'"
        return "1=1"

    # Patterns

    # 1. Broker most deals
    if ("broker" in ql or "брокер" in ql) and ("eng ko" in ql or "больше" in ql or "ko'p" in ql or "топ" in ql or "лучший" in ql or "qaysi" in ql):
        if year:
            sql = f"SELECT seller_broker AS broker, COUNT(*) AS cnt FROM deals WHERE {date_filter(year)} GROUP BY seller_broker ORDER BY cnt DESC LIMIT 5;"
        else:
            sql = "SELECT seller_broker AS broker, COUNT(*) AS cnt FROM deals GROUP BY seller_broker ORDER BY cnt DESC LIMIT 5;"
        exp = f"Brokerlar bo'yicha bitimlar soni ({year or 'barcha yillar'})" if lang=="uz" else f"Количество сделок по брокерам ({year or 'все годы'})"
        return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 2. Specific broker count
    if broker and ("nechta" in ql or "qancha" in ql or "сколько" in ql or "count" in ql or "bitim" in ql or "сдел" in ql):
        sql = f"SELECT COUNT(*) AS cnt FROM deals WHERE seller_broker='{broker}' OR buyer_broker='{broker}';"
        if year:
            sql = f"SELECT COUNT(*) AS cnt FROM deals WHERE (seller_broker='{broker}' OR buyer_broker='{broker}') AND {date_filter(year)};"
        exp = f"{broker} brokeri ishtirokidagi bitimlar soni" if lang=="uz" else f"Количество сделок с участием брокера {broker}"
        return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 3. INN based queries (buyer or seller)
    if inn:
        # Determine if buyer or seller mention
        if any(w in ql for w in ["sotuvchi", "продавец", "sotgan", "продал"]):
            col = "seller_inn"
        elif any(w in ql for w in ["xaridor", "покупатель", "olgan", "купил", "xarid"]):
            col = "buyer_inn"
        else:
            # Check question contains "xarid" or "продаж" context? Default to buyer if ambiguous but try both via OR? Better ask LLM, but heuristic: try buyer
            # For generic "qancha bitim" we count both
            if "qancha" in ql or "сколько" in ql or "jami" in ql or "сумма" in ql or "summa" in ql:
                # total sum/count for this INN as either side
                if any(x in ql for x in ["summa", "сумма", "total", "jami", "общая"]):
                    # sum query
                    if year:
                        sql = f"SELECT SUM(deal_sum) AS total_sum, COUNT(*) AS cnt FROM deals WHERE (seller_inn='{inn}' OR buyer_inn='{inn}') AND {date_filter(year)} AND deal_status != 'Аннулирован(Не оплачен)';"
                    else:
                        sql = f"SELECT SUM(deal_sum) AS total_sum, COUNT(*) AS cnt FROM deals WHERE seller_inn='{inn}' OR buyer_inn='{inn}';"
                    exp = f"STIR {inn} bo'yicha jami summa va bitimlar soni" if lang=="uz" else f"Общая сумма и количество сделок по ИНН {inn}"
                    return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}
                else:
                    if year:
                        sql = f"SELECT COUNT(*) AS cnt FROM deals WHERE (seller_inn='{inn}' OR buyer_inn='{inn}') AND {date_filter(year)};"
                    else:
                        sql = f"SELECT COUNT(*) AS cnt FROM deals WHERE seller_inn='{inn}' OR buyer_inn='{inn}';"
                    exp = f"STIR {inn} bo'yicha bitimlar soni" if lang=="uz" else f"Количество сделок по ИНН {inn}"
                    return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}
            else:
                col = "buyer_inn"
        # Simple count/sum with year filter
        if any(w in ql for w in ["summa", "сумма", "total", "jami"]):
            if year:
                sql = f"SELECT SUM(deal_sum) AS total_sum FROM deals WHERE {col}='{inn}' AND {date_filter(year)} AND deal_status != 'Аннулирован(Не оплачен)';"
            else:
                sql = f"SELECT SUM(deal_sum) AS total_sum FROM deals WHERE {col}='{inn}';"
            exp = f"STIR {inn} bo'yicha jami summa ({col})" if lang=="uz" else f"Общая сумма по ИНН {inn} ({col})"
        else:
            if year:
                sql = f"SELECT COUNT(*) AS cnt, SUM(deal_sum) AS total FROM deals WHERE {col}='{inn}' AND {date_filter(year)};"
            else:
                sql = f"SELECT COUNT(*) AS cnt FROM deals WHERE {col}='{inn}';"
            exp = f"STIR {inn} bo'yicha ma'lumotlar" if lang=="uz" else f"Данные по ИНН {inn}"
        return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 4. Product based most expensive/cheapest
    if product and any(w in ql for w in ["eng qimmat", "eng arzon", "самый дорогой", "самый дешевый", "дорог", "дешев", "qimmat", "arzon", "min", "max"]):
        is_max = any(w in ql for w in ["qimmat", "дорог", "max", "высокая", "юқори"])
        is_min = any(w in ql for w in ["arzon", "дешев", "дешёвый", "past", "низкая", "min"])
        order = "DESC" if is_max and not is_min else "ASC"
        if "arzon" in ql and "qimmat" in ql or "дорог" in ql and "дешев" in ql:
            # both cheapest and most expensive
            sql = f"SELECT deal_id, deal_date, product_name, (deal_sum/(quantity*lot)) AS unit_price, deal_sum, quantity, lot, unit FROM deals WHERE product_name LIKE '%{product}%' COLLATE NOCASE AND {date_filter(year)} ORDER BY unit_price ASC LIMIT 1;"
            exp = f"{product} bo'yicha eng arzon bitim" if lang=="uz" else f"Самая дешёвая сделка по {product}"
            return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}
        else:
            sql = f"SELECT deal_id, deal_date, product_name, (deal_sum/(quantity*lot)) AS unit_price, deal_sum, quantity, unit FROM deals WHERE product_name LIKE '%{product}%' COLLATE NOCASE AND {date_filter(year)} ORDER BY unit_price {order} LIMIT 5;"
            exp = f"{product} bo'yicha {'eng qimmat' if order=='DESC' else 'eng arzon'} bitimlar" if lang=="uz" else f"Самые {'дорогие' if order=='DESC' else 'дешёвые'} сделки по {product}"
            return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 5. Product count
    if product and any(w in ql for w in ["nechta", "qancha", "сколько", "count", "soni"]):
        sql = f"SELECT COUNT(*) AS cnt, SUM(deal_sum) AS total FROM deals WHERE product_name LIKE '%{product}%' COLLATE NOCASE AND {date_filter(year)};"
        exp = f"{product} mahsuloti bo'yicha bitimlar soni va summasi" if lang=="uz" else f"Количество и сумма сделок по {product}"
        return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 6. Total deals per year
    if any(w in ql for w in ["jami bitim", "всего сделок", "общее количество", "nechta bitim",
                              "сколько сделок", "сделок было", "количество сделок", "bitimlar soni"]):
        region_clause = f" AND region LIKE '%{region_found}%'" if region_found else ""
        if year:
            sql = f"SELECT COUNT(*) AS cnt, SUM(deal_sum) AS total_sum FROM deals WHERE {date_filter(year)}{region_clause};"
        elif region_found:
            sql = f"SELECT COUNT(*) AS cnt, SUM(deal_sum) AS total_sum FROM deals WHERE region LIKE '%{region_found}%';"
        else:
            # by year
            sql = "SELECT strftime('%Y', deal_date) AS yil, COUNT(*) AS cnt, SUM(deal_sum) AS total FROM deals GROUP BY strftime('%Y', deal_date) ORDER BY yil;"
        exp = f"{year or 'Yillar'} bo'yicha jami bitimlar" if lang=="uz" else f"Всего сделок за {year or 'все годы'}"
        return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 7. Export deals
    if "eksport" in ql or "экспорт" in ql:
        if year:
            sql = f"SELECT COUNT(*) AS cnt, SUM(deal_sum_uzs) AS total_uzs FROM deals_enriched WHERE contract_type='Экспортный' AND {date_filter(year)};"
        else:
            sql = "SELECT contract_type, COUNT(*) AS cnt, SUM(deal_sum_uzs) AS total_uzs FROM deals_enriched GROUP BY contract_type;"
        exp = "Eksport bitimlari soni va so'mdagi summasi" if lang=="uz" else "Количество и сумма экспортных сделок в сумах"
        return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 8. Top products
    if any(w in ql for w in ["eng ko'p sotilgan", "популярный товар", "топ товар", "eng ko'p bitim"]):
        if year:
            sql = f"SELECT product_name, COUNT(*) AS cnt FROM deals WHERE {date_filter(year)} GROUP BY product_name ORDER BY cnt DESC LIMIT 10;"
        else:
            sql = "SELECT product_name, COUNT(*) AS cnt FROM deals GROUP BY product_name ORDER BY cnt DESC LIMIT 10;"
        exp = "Eng ko'p sotilgan tovarlar" if lang=="uz" else "Самые популярные товары"
        return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 9. Region (region_found computed earlier)
    if region_found and any(w in ql for w in ["region", "viloyat", "область", "hudud"]):
        sql = f"SELECT COUNT(*) AS cnt FROM deals WHERE region LIKE '%{region_found}%' AND {date_filter(year)};"
        exp = f"{region_found} bo'yicha bitimlar" if lang=="uz" else f"Сделки по региону {region_found}"
        return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 9b. TOP buyer / seller (крупнейший покупатель / eng yirik xaridor)
    is_buyer = any(w in ql for w in ["xaridor", "покупател", "xarid", "покуп", "olgan", "купил"])
    is_seller = any(w in ql for w in ["sotuvchi", "продавец", "sotgan", "продал", "поставщик"])
    if (is_buyer or is_seller) and any(w in ql for w in ["eng ko", "eng yirik", "крупнейш", "топ", "top", "больше всего", "наибольш", "лидер", "eng katta"]):
        col = "buyer_inn" if is_buyer and not is_seller else "seller_inn"
        sql = (f"SELECT p.name AS company, COUNT(*) AS cnt, SUM(d.deal_sum) AS total "
               f"FROM deals d JOIN participants p ON d.{col}=p.inn "
               f"WHERE {date_filter(year)} AND d.deal_status != 'Аннулирован(Не оплачен)' "
               f"GROUP BY d.{col}, p.name ORDER BY total DESC LIMIT 3;")
        exp = ("Eng yirik " + ("xaridor" if col=="buyer_inn" else "sotuvchi") + "lar (aylanma bo'yicha)") if lang=="uz" else               ("Крупнейшие " + ("покупатели" if col=="buyer_inn" else "продавцы") + " (по обороту)")
        return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 9c. Turnover / total sum (объём / оборот / aylanma / jami qancha)
    if any(w in ql for w in ["объем", "объём", "оборот", "aylanma", "jami qancha", "общая сумма", "qancha so'm", "qancha som", "сколько сум"]):
        where = [date_filter(year), "deal_status != 'Аннулирован(Не оплачен)'"]
        if product:
            where.append(f"ufold(product_name) LIKE ufold('%{product}%')")
        if region_found:
            where.append(f"region LIKE '%{region_found}%'")
        sql = f"SELECT COUNT(*) AS cnt, SUM(deal_sum_uzs) AS total_uzs FROM deals_enriched WHERE {' AND '.join(where)};"
        exp = "Jami aylanma (so'mda)" if lang=="uz" else "Общий оборот (в сумах)"
        return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # 10. General fallback: try product LIKE with first keyword
    # Extract first meaningful word (>3 chars)
    words = re.findall(r"\b\w{4,}\b", question)
    if words:
        kw = words[0]
        # avoid stopwords
        stop = {"qancha", "qaysi", "nechta", "qachon", "qayer", "uchun", "bo'yicha", "сколько", "какой", "какая"}
        if kw.lower() not in stop:
            sql = f"SELECT deal_id, deal_date, product_name, deal_sum, quantity, unit FROM deals WHERE product_name LIKE '%{kw}%' COLLATE NOCASE LIMIT 10;"
            exp = f"'{kw}' kalit so'zi bo'yicha qidiruv" if lang=="uz" else f"Поиск по ключевому слову '{kw}'"
            # Only if looks like product query
            if any(w in ql for w in ["tovar", "товар", "mahsulot", "produkt", "bitim", "сделк"]):
                return {"sql": sql, "explanation": exp, "language": lang, "needs_data": True}

    # Default: not understood as DB query -> greeting or help
    if any(w in ql for w in ["salom", "hello", "привет", "kim san", "кто ты"]):
        return {"sql": "", "explanation": "Greeting", "language": lang, "needs_data": False}

    # If nothing matched but question looks like data question (contains year, INN pattern, etc.) we already handled.
    # Otherwise, we can still try LLM? But heuristic says no SQL
    return {"sql": "", "explanation": "Heuristic topa olmadi — LLM kerak", "language": lang, "needs_data": False}

