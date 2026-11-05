"""
UZEX AI — database helper.

* Read-only, single-SELECT SQL execution with timeout + row cap (anti-injection).
* Entity search across ALL FTS tables: participants, products AND brokers
  (audit fix #5 — previously only fts_participants was searched).
* Entity regexes: STIR/INN (9 digits), broker codes (BKxxx), contract numbers
  (10xxxx) AND deal numbers (e.g. 604000) — audit fix #5.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("UZEX_DB_PATH", str(Path(__file__).parent / "uzex.db"))

CANCELLED = "Аннулирован(Не оплачен)"

# ---------------------------------------------------------------------------
# SQL safety
# ---------------------------------------------------------------------------
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|REPLACE|"
    r"VACUUM|REINDEX|GRANT|REVOKE|TRIGGER|ANALYZE|LOAD_EXTENSION)\b",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.DOTALL)


def validate_sql(sql: str) -> tuple[bool, str]:
    """Allow exactly one read-only SELECT (or WITH ... SELECT) statement."""
    if not sql or not sql.strip():
        return False, "Empty SQL"
    cleaned = _COMMENT.sub(" ", sql).strip().rstrip(";").strip()
    if ";" in cleaned:
        return False, "Multiple statements are not allowed"
    if not re.match(r"^(SELECT|WITH)\b", cleaned, re.IGNORECASE):
        return False, "Only SELECT queries are allowed"
    if _FORBIDDEN.search(cleaned):
        return False, "Forbidden keyword in SQL"
    return True, ""


class SQLTimeout(Exception):
    pass


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def run_sql(sql: str, max_rows: int = 100, timeout_s: float = 10.0,
            db_path: str | None = None) -> dict:
    """Validate + execute a read-only query. Returns columns/rows/truncated."""
    ok, reason = validate_sql(sql)
    if not ok:
        raise ValueError(f"Unsafe SQL rejected: {reason}")
    cleaned = _COMMENT.sub(" ", sql).strip().rstrip(";").strip()
    if not re.search(r"\bLIMIT\s+\d+", cleaned, re.IGNORECASE):
        cleaned += f" LIMIT {max_rows + 1}"
    con = get_connection(db_path)
    steps = [0]

    def guard() -> int:
        steps[0] += 1
        if steps[0] % 20000 == 0:
            import time
            if time.monotonic() - start > timeout_s:
                return 1
        return 0

    import time
    start = time.monotonic()
    con.set_progress_handler(guard, 1000)
    try:
        cur = con.execute(cleaned)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchmany(max_rows + 1)]
        truncated = len(rows) > max_rows
        return {"columns": cols, "rows": rows[:max_rows], "truncated": truncated}
    except sqlite3.OperationalError as e:
        if "interrupt" in str(e).lower():
            raise SQLTimeout(f"Query exceeded {timeout_s}s") from e
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Meta / schema
# ---------------------------------------------------------------------------
def get_meta(db_path: str | None = None) -> dict:
    con = get_connection(db_path)
    try:
        return {k: v for k, v in con.execute("SELECT key, value FROM meta")}
    finally:
        con.close()


SCHEMA_TEXT = f"""
TABLE deals (500 000 rows, one row per exchange deal, 2022-01-03 .. 2026-10-30):
  deal_number INTEGER (e.g. 604000), deal_date TEXT 'YYYY-MM-DD', deal_time TEXT,
  auction_type TEXT ('Английский аукцион'|'Обратный аукцион'|'Двусторонний аукцион'),
  quantity REAL (lots bought), amount REAL (in contract currency),
  currency TEXT ('UZS'|'USD'), usd_rate REAL, amount_uzs REAL (ALWAYS in UZS; USD already converted),
  start_price REAL, contract_number INTEGER (e.g. 100001), product_name TEXT (Russian, e.g. 'Цемент портландский'),
  lot REAL, unit TEXT (тонна/килограмм/шт/литр/метр...), volume REAL (=quantity*lot, in units),
  unit_price REAL (=amount/volume, contract currency),
  region TEXT (UZBEK LATIN: 'Toshkent shahri','Navoiy viloyati','Farg‘ona viloyati',...),
  contract_type TEXT ('Внутренний'|'Экспортный'), form TEXT ('Спот'|'Форвард'|'Фьючерс'),
  status TEXT ('Новый'|'Не подписан'|'Заключен'|'Ждёт оплату'|'Оплачен'|'Исполнен'|'Исполнен(Частично)'|'{CANCELLED}'),
  seller_inn TEXT (9 digits), buyer_inn TEXT, seller_broker TEXT ('BK001'...), buyer_broker TEXT,
  payment_date TEXT, delivery_date TEXT, delivered REAL.
TABLE contracts (6616 rows): contract_number, product_name, brand, size, grade, type, class,
  tnved, product_group, category, industry, unit, lot, currency ('UZS'|'USD'), base_price,
  deposit_pct, delivery_basis, region (RUSSIAN names here), warehouse, auction_type,
  contract_type, form, payment_days, delivery_days, packaging, seller_inn, reg_date, valid_until.
TABLE participants (14392): inn TEXT PK, name TEXT, region TEXT, activity TEXT, role TEXT, vat_payer TEXT.
TABLE brokers (120): broker_code TEXT PK ('BK001'...), name TEXT, inn TEXT, region TEXT.
TABLE usd_rates (1765): rate_date TEXT PK, rate REAL (UZS per 1 USD).
"""


# ---------------------------------------------------------------------------
# Language helpers: transliteration + synonyms (UZ latin <-> RU cyrillic)
# ---------------------------------------------------------------------------
_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "",
    "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Uzbek (latin) product term -> Russian substrings as stored in the DB.
PRODUCT_SYNONYMS: dict[str, list[str]] = {
    "sement": ["цемент", "портландцемент"],
    "bug'doy": ["пшеница"], "bugdoy": ["пшеница"], "bug‘doy": ["пшеница"],
    "un": ["мука"],
    "guruch": ["рис"],
    "shakar": ["сахар"],
    "tuz": ["соль"],
    "paxta": ["хлопок"],
    "yog'": ["масло"], "yog‘": ["масло"], "yog": ["масло"], "moy": ["масло"],
    "kungaboqar": ["подсолнечн"],
    "benzin": ["бензин"],
    "dizel": ["дизель"],
    "mazut": ["мазут"],
    "bitum": ["битум"],
    "gips": ["гипс"],
    "ohak": ["известь"],
    "karbamid": ["карбамид", "мочевина"],
    "selitra": ["селитра"],
    "kislota": ["кислота"],
    "soda": ["сода"],
    "armatura": ["арматура"],
    "alyuminiy": ["алюмин"],
    "mis": ["медн", "катод"],
    "katod": ["катод"],
    "po'lat": ["стал"], "polat": ["стал"], "po‘lat": ["стал"],
    "list": ["лист"],
    "kabel": ["кабель"],
    "sim": ["проволока"],
    "podshipnik": ["подшипник"],
    "polietilen": ["полиэтилен"],
    "polipropilen": ["полипропилен"],
    "pvx": ["поливинилхлорид"],
    "truba": ["труб"],
    "g'isht": ["кирпич"], "gisht": ["кирпич"], "g‘isht": ["кирпич"],
    "taxta": ["пиломатериал"], "yog'och": ["пиломатериал"], "yogoch": ["пиломатериал"],
    "shisha": ["стекло"], "oyna": ["стекло"],
    "emal": ["эмаль"],
    "shrot": ["шрот"],
    "kunjara": ["шрот"],
    "kombikorm": ["комбикорм", "кормосмесь"],
    "yem": ["комбикорм", "кормосмесь"],
    "suv": ["вода"],
    "azot": ["азот"],
    "argon": ["аргон"],
    "ip": ["пряжа"], "kalava": ["пряжа"],
    "gaz": ["газ", "пропан"],
    "propan": ["пропан"],
    "o'g'it": ["карбамид", "селитра"], "ogit": ["карбамид", "селитра"],
}

# Russian region name (as users type) -> deals.region value (Uzbek latin).
REGION_MAP: dict[str, str] = {
    "андижан": "Andijon viloyati", "andijon": "Andijon viloyati",
    "бухар": "Buxoro viloyati", "buxoro": "Buxoro viloyati",
    "ферган": "Farg‘ona viloyati", "farg'ona": "Farg‘ona viloyati",
    "fargona": "Farg‘ona viloyati", "farg‘ona": "Farg‘ona viloyati",
    "джизак": "Jizzax viloyati", "jizzax": "Jizzax viloyati",
    "наманган": "Namangan viloyati", "namangan": "Namangan viloyati",
    "навои": "Navoiy viloyati", "navoiy": "Navoiy viloyati", "navoi": "Navoiy viloyati",
    "кашкадар": "Qashqadaryo viloyati", "qashqadaryo": "Qashqadaryo viloyati",
    "каракалпак": "Qoraqalpog‘iston Respublikasi", "qoraqalpog": "Qoraqalpog‘iston Respublikasi",
    "самарканд": "Samarqand viloyati", "samarqand": "Samarqand viloyati",
    "сырдар": "Sirdaryo viloyati", "sirdaryo": "Sirdaryo viloyati",
    "сурхандар": "Surxondaryo viloyati", "surxondaryo": "Surxondaryo viloyati",
    "хорезм": "Xorazm viloyati", "xorazm": "Xorazm viloyati",
    "ташкент": "Toshkent", "toshkent": "Toshkent",
}


def cyr_to_lat(text: str) -> str:
    return "".join(_CYR_TO_LAT.get(ch, ch) for ch in text.lower())


def normalize_apostrophes(text: str) -> str:
    return (text.replace("ʻ", "'").replace("ʼ", "'").replace("‘", "'")
                .replace("’", "'").replace("`", "'").replace("´", "'"))


def product_patterns(term: str) -> list[str]:
    """All LIKE substrings to try for a (possibly Uzbek) product term."""
    t = normalize_apostrophes(term.lower()).strip()
    pats = [t]
    for key, vals in PRODUCT_SYNONYMS.items():
        k = normalize_apostrophes(key)
        if k == t or (len(k) >= 4 and k in t):
            pats.extend(vals)
    return list(dict.fromkeys(pats))


def region_value(text: str) -> str | None:
    """Map a region mention (UZ or RU) to the canonical deals.region value."""
    t = normalize_apostrophes(text.lower())
    for key, val in REGION_MAP.items():
        if normalize_apostrophes(key) in t:
            if val == "Toshkent":
                # city vs oblast
                if any(w in t for w in ("город", "шахр", "shahri", "shahar", "г. ташкент")):
                    return "Toshkent shahri"
                if any(w in t for w in ("област", "viloyat")):
                    return "Toshkent viloyati"
                return "Toshkent"  # prefix — caller uses LIKE
            return val
    return None


# ---------------------------------------------------------------------------
# Entity extraction + FTS search (fix #5: ALL three FTS tables + deal numbers)
# ---------------------------------------------------------------------------
RE_INN = re.compile(r"\b\d{9}\b")
RE_BROKER = re.compile(r"\bBK\d{3}\b", re.IGNORECASE)
RE_CONTRACT = re.compile(r"\b10\d{4}\b")
RE_DEAL = re.compile(r"\b\d{6}\b")

_STOPWORDS = set("""
the and for with what which who how many much total sum average top all
qanday qaysi nechta qancha haqida boyicha bo'yicha kim bormi jami narxi narx
bitim bitimlar sotuvchi xaridor savdo aylanma aylanmasi viloyat viloyatida
shartnoma korxona tovar eng katta kichik yirik yil yilda oy oyida nechchi
какой какая какие сколько сумма общий всего средний топ все по за из на и в
что кто как когда где компания компании продавец покупатель товар товары
сделка сделки сделок оборот год году месяц регион область город цена цены
самый самая наиболее больше меньше его её их это тот эта те
""".split())


def _fts_query(tokens: list[str]) -> str | None:
    parts = []
    for t in tokens:
        t = re.sub(r"[^\w\u0400-\u04FF‘'’-]", "", t)
        if len(t) >= 3 and t.lower() not in _STOPWORDS:
            parts.append('"' + t.replace('"', '') + '"*')
    return " OR ".join(parts) if parts else None


def search_entities(question: str, db_path: str | None = None, limit: int = 6) -> dict:
    """Find companies, products, brokers, contracts, deals mentioned in text.

    Searches fts_participants, fts_products AND fts_brokers (fix #5), plus
    exact regexes for INN / broker code / contract number / deal number.
    """
    q = normalize_apostrophes(question)
    found: dict = {"participants": [], "products": [], "brokers": [],
                   "contracts": [], "deals": [], "inns": [], "regions": []}
    con = get_connection(db_path)
    try:
        # --- exact identifiers -------------------------------------------
        for inn in RE_INN.findall(q):
            found["inns"].append(inn)
            row = con.execute("SELECT inn, name, region, role FROM participants WHERE inn=?", (inn,)).fetchone()
            if row:
                found["participants"].append(dict(row))
        for code in {c.upper() for c in RE_BROKER.findall(q)}:
            row = con.execute("SELECT broker_code, name, region FROM brokers WHERE broker_code=?", (code,)).fetchone()
            if row:
                found["brokers"].append(dict(row))
        for cn in set(RE_CONTRACT.findall(q)):
            row = con.execute(
                "SELECT contract_number, product_name, currency, base_price, seller_inn FROM contracts WHERE contract_number=?",
                (int(cn),)).fetchone()
            if row:
                found["contracts"].append(dict(row))
        for dn in set(RE_DEAL.findall(q)):
            if RE_CONTRACT.fullmatch(dn):
                continue
            row = con.execute(
                "SELECT deal_number, deal_date, product_name, amount_uzs, status FROM deals WHERE deal_number=?",
                (int(dn),)).fetchone()
            if row:
                found["deals"].append(dict(row))

        # --- region mentions ----------------------------------------------
        rv = region_value(q)
        if rv:
            found["regions"].append(rv)

        # --- FTS across all three tables -----------------------------------
        tokens = re.findall(r"[\w\u0400-\u04FF'’‘-]+", q)
        # add Russian product synonyms for Uzbek terms
        extra: list[str] = []
        for tok in tokens:
            tl = normalize_apostrophes(tok.lower())
            for key, vals in PRODUCT_SYNONYMS.items():
                if normalize_apostrophes(key) == tl or (len(key) >= 4 and normalize_apostrophes(key) in tl):
                    extra.extend(vals)
        match = _fts_query(tokens + extra)
        if match:
            try:
                for r in con.execute(
                        "SELECT name, inn, region FROM fts_participants WHERE fts_participants MATCH ? LIMIT ?",
                        (match, limit)):
                    d = dict(r)
                    if d not in found["participants"]:
                        found["participants"].append(d)
            except sqlite3.OperationalError:
                pass
            try:
                for r in con.execute(
                        "SELECT product_name, contract_number FROM fts_products WHERE fts_products MATCH ? LIMIT ?",
                        (match, limit)):
                    d = dict(r)
                    if d not in found["products"]:
                        found["products"].append(d)
            except sqlite3.OperationalError:
                pass
            try:
                for r in con.execute(
                        "SELECT name, broker_code, region FROM fts_brokers WHERE fts_brokers MATCH ? LIMIT ?",
                        (match, limit)):
                    d = dict(r)
                    if d not in found["brokers"]:
                        found["brokers"].append(d)
            except sqlite3.OperationalError:
                pass

        # --- LIKE fallback for products (synonyms, case-insensitive) --------
        if not found["products"]:
            for tok in tokens:
                if len(tok) < 3 or tok.lower() in _STOPWORDS:
                    continue
                for pat in product_patterns(tok):
                    if len(pat) < 3:
                        continue
                    rows = con.execute(
                        "SELECT DISTINCT product_name, contract_number FROM fts_products WHERE LOWER(product_name) LIKE ? LIMIT ?",
                        (f"%{pat.lower()}%", limit)).fetchall()
                    for r in rows:
                        d = dict(r)
                        if d not in found["products"]:
                            found["products"].append(d)
                    if found["products"]:
                        break
                if found["products"]:
                    break
        return found
    finally:
        con.close()


def entities_context(found: dict) -> str:
    """Compact context block for the LLM prompt."""
    lines = []
    if found.get("inns"):
        lines.append("STIR/INN mentioned: " + ", ".join(found["inns"]))
    if found.get("participants"):
        lines.append("Matching companies (participants):")
        for p in found["participants"][:6]:
            lines.append(f"  - inn={p['inn']} name={p['name']} region={p.get('region','')}")
    if found.get("products"):
        lines.append("Matching products (use these EXACT product_name values in LIKE filters):")
        for p in found["products"][:8]:
            lines.append(f"  - {p['product_name']}")
    if found.get("brokers"):
        lines.append("Matching brokers:")
        for b in found["brokers"][:6]:
            lines.append(f"  - {b['broker_code']} {b['name']}")
    if found.get("contracts"):
        lines.append("Matching contracts:")
        for c in found["contracts"][:4]:
            lines.append(f"  - #{c['contract_number']} {c['product_name']} currency={c['currency']} base_price={c['base_price']}")
    if found.get("deals"):
        lines.append("Matching deals:")
        for d in found["deals"][:4]:
            lines.append(f"  - #{d['deal_number']} {d['deal_date']} {d['product_name']} amount_uzs={d['amount_uzs']} status={d['status']}")
    if found.get("regions"):
        lines.append("Region filter value(s): " + ", ".join(found["regions"]))
    return "\n".join(lines) if lines else "(no exact entities found in the question)"
