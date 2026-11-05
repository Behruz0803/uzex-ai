"""
UZEX AI — deterministic local Text-to-SQL engine.

Used when:
  * GEMINI_API_KEY is not configured (the prototype still works for judges), or
  * the Gemini API is down / rate-limited (audit fix #1 — no ugly 503 errors).

It parses UZ/RU questions into a structured intent and builds safe SELECTs.
It also powers the deterministic "near match" relaxation and local answer
formatting, so the bot never hallucinates: every number comes from SQLite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import db_helper as db

MONTHS_UZ = {
    "yanvar": 1, "fevral": 2, "mart": 3, "aprel": 4, "may": 5, "iyun": 6,
    "iyul": 7, "avgust": 8, "sentabr": 9, "oktabr": 10, "noyabr": 11, "dekabr": 12,
}
MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

_DIM_WORDS = {
    "seller": ["sotuvchi", "sotuvchilar", "продавец", "продавц", "поставщик"],
    "buyer": ["xaridor", "xaridorlar", "покупател", "харидор"],
    "broker": ["broker", "брокер"],
    "region": ["viloyat", "hudud", "регион", "област", "город", "shahar"],
    "product": ["tovar", "mahsulot", "товар", "продукт"],
}


@dataclass
class Plan:
    sql: str
    intent: str
    metric: str
    filters: dict = field(default_factory=dict)
    dimension: str | None = None
    limit: int = 10


def _q(s: str) -> str:
    """Escape a SQL string literal."""
    return s.replace("'", "''")


# Russian function-words that must never be treated as product names.
_RU_STOP_PREFIXES = (
    "сделк", "бирж", "скольк", "как", "компан", "брокер", "товар", "продукт",
    "регион", "област", "город", "год", "месяц", "сумм", "общ", "средн", "сам",
    "больш", "всег", "цен", "продав", "покупат", "экспорт", "валют", "доллар",
    "аннулир", "отмен", "форвард", "фьючерс", "спот", "январ", "феврал", "март",
    "апрел", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр",
    "крупн", "наибол", "лидер", "рейтинг", "топ", "через", "был", "заключ",
    "соверш", "провед", "продал", "купил", "врем", "всё", "все", "объем", "объём",
    "оборот", "какой", "какая", "какие", "этот", "эта", "эти", "того", "этого",
    "наибольш", "максим", "миним", "дорог", "дешев", "единиц", "измерен",
    "количеств", "число", "статус", "состоян", "внутрен", "долларов", "сумов",
    "продаж", "покуп", "реализац", "поставк", "оплат", "исполн", "подписан",
)


def _ru_is_stop(tok: str) -> bool:
    return tok in db._STOPWORDS or any(tok.startswith(p) for p in _RU_STOP_PREFIXES)


def _ru_stem(tok: str) -> str:
    """Very light Russian stemming so 'цементу' -> 'цемент'."""
    for end in ("ами", "ями", "ов", "ев", "ам", "ям", "ах", "ях", "ой", "ий",
                "ая", "ое", "ые", "ие", "ую", "юю", "ом", "ем", "у", "ю", "а",
                "я", "ы", "и", "е", "й", "ь"):
        if tok.endswith(end) and len(tok) - len(end) >= 4:
            return tok[: -len(end)]
    return tok


_UZ_STOP_EXTRA = set("""
tovar mahsulot bitim bitimlar bitimlarni qanday qaysi nechta qancha boyicha
bormi viloyat shahar shahrida yilda yili narxi narx savdo aylanma aylanmasi
jami sotuvchi xaridor kompaniya korxona broker shartnoma eng katta yirik
qimmat arzon ortacha top ekan bolgan qilgan tuzilgan barcha davrlar davr
yicha aylanmaga har bir ikki uch tort besh olti yetti sakkiz toqqiz on
yuz ming milliard trillion so'm som sum uzs usd dollar valyuta eksport import
bekor qilingan holat status forvard spot fyuchers stir inn kod raqam hisobot
statistika tahlil reyestr birja oxirgi birinchi ikkinchi korsat chiqar ber
kop kam marta nechchi mavjud bor yoq yo'q nimadir nima qayerda qachon kim
""".split())


def _uz_is_stop(tok: str) -> bool:
    return (tok in db._STOPWORDS or tok in _UZ_STOP_EXTRA
            or tok in db.PRODUCT_SYNONYMS or tok in db.REGION_MAP
            or tok in MONTHS_UZ)


class FallbackEngine:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or db.DB_PATH

    # ------------------------------------------------------------------ parse
    def parse(self, question: str) -> dict:
        raw = question
        q = db.normalize_apostrophes(question.lower())
        f: dict = {}

        # identifiers
        deal_m = db.RE_DEAL.findall(q)
        con_m = db.RE_CONTRACT.findall(q)
        inn_m = db.RE_INN.findall(q)
        br_m = db.RE_BROKER.findall(q)
        if deal_m:
            con = db.get_connection(self.db_path)
            for dn in deal_m:
                if dn in con_m:
                    continue
                if con.execute("SELECT 1 FROM deals WHERE deal_number=?", (int(dn),)).fetchone():
                    f["deal"] = int(dn)
                    break
            con.close()
        if con_m:
            f["contract"] = int(con_m[0])
        if inn_m:
            f["inn"] = inn_m[0]
        if br_m:
            f["broker"] = br_m[0].upper()

        # year / month
        ym = re.search(r"\b(20\d{2})\b", q)
        if ym:
            f["year"] = int(ym.group(1))
        for name, m in {**MONTHS_UZ, **MONTHS_RU}.items():
            if re.search(r"\b" + name, q):
                f["month"] = m
                break

        # region
        rv = db.region_value(q)
        if rv:
            f["region"] = rv
        elif re.search(r"(viloyat|shahar|shahr|област|город)", q):
            m = re.search(r"([a-z\u0400-\u04FF'’‘-]+)\s+(?:viloyat\w*|shahar\w*|shahr\w*|област\w*|город\w*)", q)
            if m and m.group(1) not in ("bu", "qaysi", "какой", "какая", "этот", "har", "qaysi"):
                f["region_like"] = m.group(1)

        # product
        pats: list[str] = []
        for key, vals in db.PRODUCT_SYNONYMS.items():
            k = db.normalize_apostrophes(key)
            if re.search(r"(?<![\w'])" + re.escape(k) + r"(?![\w'])", q) or (len(k) >= 5 and k in q):
                pats.extend(vals)
        # cyrillic product words straight from the question (with light stemming)
        for tok in re.findall(r"[а-яё]{4,}", q):
            if _ru_is_stop(tok):
                continue
            stem = _ru_stem(tok)
            if len(stem) >= 4:
                pats.append(stem)
        # unknown latin product names (Bitcoin, iPhone ...): only when an
        # explicit product noun is present OR two unknowns are joined by yoki/или
        if not pats:
            explicit = re.search(r"\b(tovar|mahsulot|товар\w*|продукт\w*)\b", q)
            pair = re.search(r"\b([a-z]{4,})\s+(?:yoki|ili|или)\s+([a-z]{4,})\b", q)
            cands: list[str] = []
            if pair:
                cands = [pair.group(1), pair.group(2)]
            elif explicit:
                cands = re.findall(r"[a-z]{4,}", q)
            for tok in cands:
                if _uz_is_stop(tok):
                    continue
                pats.append(tok)
        if pats:
            f["product_patterns"] = list(dict.fromkeys(pats))[:6]

        # status / type / form
        if re.search(r"(bekor|аннулир|отмен)", q):
            f["status"] = db.CANCELLED
        if re.search(r"(eksport|экспорт)", q):
            f["ctype"] = "Экспортный"
        elif re.search(r"(ichki|внутренн)", q):
            f["ctype"] = "Внутренний"
        if re.search(r"(forvard|форвард)", q):
            f["form"] = "Форвард"
        elif re.search(r"(fyuchers|фьючерс)", q):
            f["form"] = "Фьючерс"
        elif re.search(r"(\bspot\b|спот)", q):
            f["form"] = "Спот"

        # metric
        if re.search(r"(o'rtacha|ortacha|средн)", q):
            f["metric"] = "avg"
        elif re.search(r"(eng\s+qimmat|самый\s+дорогой|максимальн\w*\s+цен| highest)", q):
            f["metric"] = "max_price"
        elif re.search(r"(eng\s+arzon|самый\s+дешев|минимальн\w*\s+цен|lowest)", q):
            f["metric"] = "min_price"
        elif re.search(r"(\btop\b|eng\s+ko'p|eng\s+kop|eng\s+katta|eng\s+yirik|"
                       r"крупнейш|больше\s+всего|наибольш|лидер|рейтинг|самый\s+крупн)", q):
            f["metric"] = "top"
        elif re.search(r"(nechta|сколько|количеств|soni\b|count)", q):
            f["metric"] = "count"
        elif re.search(r"(ko'rsat|korsat|chiqar|список|покажи|последн|oxirgi|oxirgi|list)", q):
            f["metric"] = "list"
        elif re.search(r"(jami|qancha|summa|aylanma|оборот|сумма|объем|объём|sum|total)", q):
            f["metric"] = "sum"
        else:
            f["metric"] = "stats"

        # dimension for top / group
        for dim, words in _DIM_WORDS.items():
            if any(w in q for w in words):
                f["dimension"] = dim
                break

        # top N
        n = re.search(r"(?:top|топ)[\s-]*(\d{1,2})", q)
        f["limit"] = int(n.group(1)) if n else (3 if f["metric"] == "top" else 20)

        # existence wording -> count all statuses
        f["count_all"] = bool(re.search(r"(mavjud|bazada\s+jami|в\s+базе|всего\s+в\s+базе|имеется)", q))
        # broker/company questions asking BOTH count and sum -> full stats
        if (f.get("broker") or f.get("inn")) and f["metric"] == "count" and re.search(
                r"(summa|qancha|umumiy|сумм|общ|сколько\s+денег|объем|объём)", q):
            f["metric"] = "stats"
        return f

    # ------------------------------------------------------------------ build
    def build(self, question: str, lang: str = "uz") -> Plan | None:
        f = self.parse(question)
        metric = f["metric"]

        # ---- lookups -------------------------------------------------------
        if f.get("deal"):
            sql = (f"SELECT deal_number, deal_date, product_name, quantity, lot, unit, "
                   f"amount, currency, amount_uzs, status, seller_inn, buyer_inn, "
                   f"seller_broker, buyer_broker, region FROM deals WHERE deal_number={f['deal']}")
            return Plan(sql, "deal", "lookup", f)
        if f.get("contract"):
            sql = (f"SELECT contract_number, product_name, currency, base_price, unit, lot, "
                   f"seller_inn, reg_date, valid_until, region FROM contracts "
                   f"WHERE contract_number={f['contract']}")
            return Plan(sql, "contract", "lookup", f)

        where: list[str] = []
        if f.get("year"):
            if f.get("month"):
                where.append(f"d.deal_date LIKE '{f['year']}-{f['month']:02d}%'")
            else:
                where.append(f"d.deal_date LIKE '{f['year']}%'")
        if f.get("region"):
            if f["region"] == "Toshkent":
                where.append("d.region LIKE 'Toshkent%'")
            else:
                where.append(f"d.region = '{_q(f['region'])}'")
        elif f.get("region_like"):
            where.append(f"LOWER(d.region) LIKE '%{_q(f['region_like'])}%'")
        if f.get("product_patterns"):
            likes = " OR ".join(f"LOWER(d.product_name) LIKE '%{_q(p.lower())}%'"
                                for p in f["product_patterns"])
            where.append(f"({likes})")
        if f.get("broker"):
            where.append(f"(d.seller_broker = '{f['broker']}' OR d.buyer_broker = '{f['broker']}')")
        if f.get("inn"):
            where.append(f"(d.seller_inn = '{f['inn']}' OR d.buyer_inn = '{f['inn']}')")
        if f.get("ctype"):
            where.append(f"d.contract_type = '{f['ctype']}'")
        if f.get("form"):
            where.append(f"d.form = '{f['form']}'")
        if f.get("status"):
            where.append(f"d.status = '{_q(f['status'])}'")
        elif f.get("broker") or f.get("inn"):
            # Broker/company activity reports count every registered deal
            # (matches the audited behaviour for BK001: 4 587 deals).
            pass
        elif metric in ("sum", "avg", "top", "max_price", "min_price") or (
                metric in ("count", "stats") and not f.get("count_all")):
            where.append(f"d.status != '{_q(db.CANCELLED)}'")

        wsql = (" WHERE " + " AND ".join(where)) if where else ""

        # ---- company / broker stats ----------------------------------------
        if f.get("inn") and metric in ("stats", "sum", "count", "avg"):
            agg = self._agg(metric)
            sql = f"SELECT {agg} FROM deals d{wsql}"
            return Plan(sql, "company", metric, f)
        if f.get("broker") and metric in ("stats", "sum", "count", "avg"):
            agg = self._agg(metric)
            sql = f"SELECT {agg} FROM deals d{wsql}"
            return Plan(sql, "broker", metric, f)

        # ---- top / group ----------------------------------------------------
        if metric == "top":
            dim = f.get("dimension") or "seller"
            lim = max(1, min(f.get("limit", 3), 50))
            if dim == "seller":
                sql = (f"SELECT p.name AS company, COUNT(*) AS deal_count, "
                       f"SUM(d.amount_uzs) AS total_uzs FROM deals d "
                       f"JOIN participants p ON d.seller_inn = p.inn{wsql} "
                       f"GROUP BY d.seller_inn, p.name ORDER BY total_uzs DESC LIMIT {lim}")
            elif dim == "buyer":
                sql = (f"SELECT p.name AS company, COUNT(*) AS deal_count, "
                       f"SUM(d.amount_uzs) AS total_uzs FROM deals d "
                       f"JOIN participants p ON d.buyer_inn = p.inn{wsql} "
                       f"GROUP BY d.buyer_inn, p.name ORDER BY total_uzs DESC LIMIT {lim}")
            elif dim == "broker":
                sql = (f"SELECT b.broker_code, b.name AS company, COUNT(*) AS deal_count, "
                       f"SUM(d.amount_uzs) AS total_uzs FROM deals d "
                       f"JOIN brokers b ON (d.seller_broker = b.broker_code OR d.buyer_broker = b.broker_code)"
                       f"{wsql} GROUP BY b.broker_code, b.name ORDER BY deal_count DESC LIMIT {lim}")
            elif dim == "region":
                sql = (f"SELECT d.region, COUNT(*) AS deal_count, SUM(d.amount_uzs) AS total_uzs "
                       f"FROM deals d{wsql} GROUP BY d.region ORDER BY deal_count DESC LIMIT {lim}")
            else:  # product
                sql = (f"SELECT d.product_name, COUNT(*) AS deal_count, SUM(d.amount_uzs) AS total_uzs "
                       f"FROM deals d{wsql} GROUP BY d.product_name ORDER BY total_uzs DESC LIMIT {lim}")
            return Plan(sql, "top", "top", f, dimension=dim, limit=lim)

        # ---- extremes -------------------------------------------------------
        if metric in ("max_price", "min_price"):
            order = "DESC" if metric == "max_price" else "ASC"
            extra = " AND d.volume > 0 AND d.unit_price IS NOT NULL"
            sql = (f"SELECT d.deal_number, d.deal_date, d.product_name, d.unit_price, "
                   f"d.currency, d.unit, d.amount_uzs FROM deals d{wsql}{extra} "
                   f"ORDER BY d.unit_price {order} LIMIT 1")
            return Plan(sql, "extreme", metric, f)

        # ---- aggregates / list ----------------------------------------------
        if metric == "list":
            sql = (f"SELECT d.deal_number, d.deal_date, d.product_name, d.amount_uzs, d.status "
                   f"FROM deals d{wsql} ORDER BY d.deal_date DESC, d.deal_number DESC LIMIT 20")
            return Plan(sql, "list", "list", f)

        agg = self._agg(metric)
        sql = f"SELECT {agg} FROM deals d{wsql}"
        intent = "general"
        if f.get("product_patterns"):
            intent = "product"
        elif f.get("region"):
            intent = "region"
        return Plan(sql, intent, metric, f)

    @staticmethod
    def _agg(metric: str) -> str:
        if metric == "count":
            return "COUNT(*) AS deal_count"
        if metric == "avg":
            return "AVG(d.amount_uzs) AS avg_uzs"
        if metric == "sum":
            return "SUM(d.amount_uzs) AS total_uzs, COUNT(*) AS deal_count"
        return "COUNT(*) AS deal_count, SUM(d.amount_uzs) AS total_uzs, AVG(d.amount_uzs) AS avg_uzs"

    # ------------------------------------------------------------- relaxation
    def relax_sql(self, sql: str, lang: str = "uz") -> tuple[str | None, str]:
        """Deterministically broaden a query that returned nothing.

        Removes month -> year -> region filters (in that order) and reports
        what was relaxed. Returns (new_sql, description) or (None, '').
        """
        uz = lang == "uz"
        patterns = [
            (r"(?:\s+AND\s+)?(?:d\.)?deal_date\s+LIKE\s+'\d{4}-\d{2}%'",
             "oy filtri olib tashlandi" if uz else "убран фильтр месяца"),
            (r"(?:\s+AND\s+)?(?:d\.)?deal_date\s+LIKE\s+'\d{4}%'",
             "yil filtri olib tashlandi" if uz else "убран фильтр года"),
            (r"(?:\s+AND\s+)?strftime\('%Y',\s*(?:d\.)?deal_date\)\s*=\s*'\d{4}'",
             "yil filtri olib tashlandi" if uz else "убран фильтр года"),
            (r"(?:\s+AND\s+)?(?:d\.)?deal_date\s+BETWEEN\s+'[^']+'\s+AND\s+'[^']+'",
             "sana oralig'i olib tashlandi" if uz else "убран диапазон дат"),
            (r"(?:\s+AND\s+)?(?:d\.)?region\s*=\s*'[^']+'",
             "hudud filtri olib tashlandi" if uz else "убран фильтр региона"),
            (r"(?:\s+AND\s+)?LOWER\((?:d\.)?region\)\s+LIKE\s+'%[^']+%'",
             "hudud filtri olib tashlandi" if uz else "убран фильтр региона"),
            (r"(?:\s+AND\s+)?(?:d\.)?status\s*=\s*'[^']+'",
             "status filtri olib tashlandi" if uz else "убран фильтр статуса"),
        ]
        new = sql
        removed = []
        for pat, label in patterns:
            m = re.search(pat, new, re.IGNORECASE)
            if m:
                new = new[:m.start()] + new[m.end():]
                removed.append(label)
                break  # relax one constraint at a time
        if not removed:
            return None, ""
        new = re.sub(r"WHERE\s+AND\s+", "WHERE ", new, flags=re.IGNORECASE)
        new = re.sub(r"WHERE\s+(GROUP|ORDER|LIMIT|$)", r"\1", new, flags=re.IGNORECASE)
        new = re.sub(r"\s{2,}", " ", new).strip()
        ok, _ = db.validate_sql(new)
        if not ok or new == sql:
            return None, ""
        return new, ", ".join(removed)

    # --------------------------------------------------------------- describe
    def describe(self, question: str, lang: str, result: dict, relaxed: bool = False) -> str:
        """Human-readable UZ/RU rendering of a result (no invention).

        When `relaxed` is True the result comes from a broadened query, so the
        scope is described as the whole available period (not the original
        year/region that matched nothing).
        """
        cols = result["columns"]
        rows = result["rows"]
        if not rows:
            return ""
        uz = lang == "uz"
        f = self.parse(question)
        if relaxed:
            f.pop("year", None)
            f.pop("month", None)
            f.pop("region", None)
            f.pop("region_like", None)
            f["relaxed"] = True
        metric = f.get("metric", "stats")

        def money(v):
            return db_fmt(v) + (" so'm" if uz else " сум")

        # lookups
        if f.get("deal") or f.get("contract"):
            lines = []
            for row in rows[:1]:
                for c, v in zip(cols, row):
                    if v is None:
                        continue
                    label = _LABELS.get(c, c)
                    if c in ("amount_uzs",):
                        lines.append(f"• {label[uz]}: {money(v)}")
                    elif isinstance(v, (int, float)):
                        lines.append(f"• {label[uz]}: {db_fmt(v)}")
                    else:
                        lines.append(f"• {label[uz]}: {v}")
            return "\n".join(lines)

        # top lists
        if metric == "top" and len(rows) > 1 or (metric == "top" and len(cols) >= 2):
            lines = []
            for i, row in enumerate(rows, 1):
                d = dict(zip(cols, row))
                name = d.get("company") or d.get("name") or d.get("region") or d.get("product_name") or "?"
                cnt = d.get("deal_count")
                tot = d.get("total_uzs")
                extra = []
                if cnt is not None:
                    extra.append(f"{db_fmt(cnt)} ta bitim" if uz else f"{db_fmt(cnt)} сделок")
                if tot is not None:
                    extra.append(money(tot))
                lines.append(f"{i}. {name} — " + ", ".join(extra))
            return "\n".join(lines)

        # single aggregate row
        if len(rows) == 1:
            d = dict(zip(cols, rows[0]))
            cnt = d.get("deal_count")
            tot = d.get("total_uzs")
            avg = d.get("avg_uzs")
            scope = self._scope_text(f, uz)
            if metric == "count" or (cnt is not None and tot is None and avg is None):
                return (f"{scope} jami {db_fmt(cnt)} ta bitim mavjud." if uz
                        else f"{scope} всего {db_fmt(cnt)} сделок.")
            if metric == "avg" and avg is not None:
                return (f"{scope} o'rtacha bitim summasi {money(avg)}." if uz
                        else f"{scope} средняя сумма сделки — {money(avg)}.")
            if metric in ("sum", "stats", "general") or (tot is not None):
                parts = []
                if tot is not None:
                    parts.append((f"{scope} jami aylanma {money(tot)}" if uz
                                  else f"{scope} общий оборот — {money(tot)}"))
                if cnt is not None:
                    parts.append((f"({db_fmt(cnt)} ta bitim)" if uz else f"({db_fmt(cnt)} сделок)"))
                return " ".join(parts) + "."
            if metric in ("max_price", "min_price"):
                up = d.get("unit_price")
                pn = d.get("product_name", "")
                cur = d.get("currency", "UZS")
                unit = d.get("unit", "")
                word = ("eng qimmat" if metric == "max_price" else "eng arzon") if uz else \
                       ("самая высокая" if metric == "max_price" else "самая низкая")
                curs = "so'm" if cur == "UZS" else "USD"
                if not uz:
                    curs = "сум" if cur == "UZS" else "USD"
                return (f"{scope} {word} birlik narxi: {pn} — {db_fmt(up)} {curs}/{unit} "
                        f"(bitim #{d.get('deal_number')}, {d.get('deal_date')}).") if uz else \
                       (f"{scope} {word} цена за единицу: {pn} — {db_fmt(up)} {curs}/{unit} "
                        f"(сделка №{d.get('deal_number')}, {d.get('deal_date')}).")

        # generic list
        lines = []
        for row in rows[:15]:
            parts = []
            for c, v in zip(cols, row):
                if v is None:
                    continue
                if c in ("amount_uzs", "total_uzs", "avg_uzs", "unit_price"):
                    parts.append(f"{c}: {db_fmt(v)}")
                elif isinstance(v, (int, float)):
                    parts.append(f"{c}: {db_fmt(v)}")
                else:
                    parts.append(f"{c}: {v}")
            lines.append("• " + "; ".join(parts))
        return "\n".join(lines)

    @staticmethod
    def _scope_text(f: dict, uz: bool) -> str:
        _MN_UZ = {1: "yanvar", 2: "fevral", 3: "mart", 4: "aprel", 5: "may", 6: "iyun",
                  7: "iyul", 8: "avgust", 9: "sentabr", 10: "oktabr", 11: "noyabr", 12: "dekabr"}
        _MN_RU = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
                  7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"}
        bits = []
        if f.get("year"):
            if f.get("month"):
                m = f["month"]
                bits.append(f"{f['year']}-yil {_MN_UZ[m]} oyida" if uz
                            else f"{_MN_RU[m]} {f['year']} года")
            else:
                bits.append(f"{f['year']}-yilda" if uz else f"{f['year']} году")
        elif f.get("relaxed"):
            bits.append("barcha davrlar" if uz else "весь доступный период")
        if f.get("region"):
            bits.append(f["region"])
        if f.get("product_patterns"):
            bits.append(("mahsulotlar" if uz else "товары") + ": " + ", ".join(f["product_patterns"][:3]))
        if f.get("broker"):
            bits.append(f["broker"])
        if f.get("inn"):
            bits.append(f"STIR {f['inn']}")
        if not bits:
            return "Bazada" if uz else "В базе"
        s = ", ".join(bits)
        if uz:
            return s + (" bo'yicha" if not s.endswith("da") else "")
        return s + ":"


def db_fmt(v) -> str:
    if v is None:
        return "0"
    if isinstance(v, float):
        if abs(v - round(v)) < 0.005:
            v = int(round(v))
        else:
            return f"{v:,.2f}".replace(",", " ")
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    return str(v)


_LABELS = {
    "deal_number": {True: "Bitim raqami", False: "Номер сделки"},
    "deal_date": {True: "Sana", False: "Дата"},
    "product_name": {True: "Tovar", False: "Товар"},
    "quantity": {True: "Miqdori (lot)", False: "Количество (лотов)"},
    "lot": {True: "Lot", False: "Лот"},
    "unit": {True: "Birlik", False: "Единица"},
    "amount": {True: "Summa (valyuta)", False: "Сумма (валюта)"},
    "currency": {True: "Valyuta", False: "Валюта"},
    "amount_uzs": {True: "Summa", False: "Сумма"},
    "status": {True: "Holat", False: "Статус"},
    "seller_inn": {True: "Sotuvchi STIR", False: "ИНН продавца"},
    "buyer_inn": {True: "Xaridor STIR", False: "ИНН покупателя"},
    "seller_broker": {True: "Sotuvchi brokeri", False: "Брокер продавца"},
    "buyer_broker": {True: "Xaridor brokeri", False: "Брокер покупателя"},
    "region": {True: "Hudud", False: "Регион"},
    "contract_number": {True: "Shartnoma raqami", False: "Номер контракта"},
    "base_price": {True: "Bazis narxi", False: "Базисная цена"},
    "reg_date": {True: "Ro'yxatdan o'tgan sana", False: "Дата регистрации"},
    "valid_until": {True: "Amal qilish muddati", False: "Действует до"},
    "unit_price": {True: "Birlik narxi", False: "Цена за единицу"},
}
