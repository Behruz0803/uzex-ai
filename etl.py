"""
UZEX AI — ETL: CSV -> SQLite (uzex.db)

Builds a single fast SQLite database from the UZEX spot-data CSV export:
  deals_2022.csv ... deals_2026.csv, contracts.csv, participants.csv,
  brokers.csv, usd_rates.csv

Normalisation rules (from the dataset README):
  * dates DD.MM.YYYY -> ISO YYYY-MM-DD
  * numbers: thousands separated by spaces -> float
  * volume       = quantity * lot
  * unit_price   = amount / volume
  * amount_uzs   = amount * USD rate on the deal date for USD (export) contracts
  * FTS5 indexes for participants, products AND brokers (audit fix #5)

Usage:
  python etl.py [--data-dir uzex_spot_data] [--db uzex.db]
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

csv.field_size_limit(10_000_000)

SPACE_RE = re.compile(r"[\s\u00a0\u202f]+")


def num(value: str | None) -> float | None:
    """'185 176.24' -> 185176.24 ; '' -> None ; '10 %' -> 10.0"""
    if value is None:
        return None
    v = SPACE_RE.sub("", value).replace("%", "")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def date(value: str | None) -> str | None:
    """'02.01.2024' -> '2024-01-02' ; '' -> None"""
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    parts = v.split(".")
    if len(parts) != 3:
        return None
    d, m, y = parts
    return f"{y}-{m.zfill(2)}-{d.zfill(2)}"


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = OFF;

DROP TABLE IF EXISTS deals;
DROP TABLE IF EXISTS contracts;
DROP TABLE IF EXISTS participants;
DROP TABLE IF EXISTS brokers;
DROP TABLE IF EXISTS usd_rates;
DROP TABLE IF EXISTS meta;

CREATE TABLE deals (
    deal_number     INTEGER PRIMARY KEY,
    deal_date       TEXT NOT NULL,
    deal_time       TEXT,
    auction_type    TEXT,
    quantity        REAL,
    amount          REAL,           -- in contract currency
    currency        TEXT,           -- 'UZS' | 'USD'
    usd_rate        REAL,
    amount_uzs      REAL,           -- always in UZS (USD deals converted by date)
    start_price     REAL,
    contract_number INTEGER,
    product_name    TEXT,
    lot             REAL,
    unit            TEXT,
    volume          REAL,           -- quantity * lot (in measurement units)
    unit_price      REAL,           -- amount / volume (contract currency)
    region          TEXT,
    contract_type   TEXT,           -- Внутренний | Экспортный
    form            TEXT,           -- Спот | Форвард | Фьючерс
    status          TEXT,
    seller_inn      TEXT,
    buyer_inn       TEXT,
    seller_broker   TEXT,
    buyer_broker    TEXT,
    payment_date    TEXT,
    delivery_date   TEXT,
    delivered       REAL
);

CREATE TABLE contracts (
    contract_number INTEGER PRIMARY KEY,
    product_name    TEXT,
    brand           TEXT,
    size            TEXT,
    grade           TEXT,
    type            TEXT,
    class           TEXT,
    tnved           TEXT,
    product_group   TEXT,
    category        TEXT,
    industry        TEXT,
    unit            TEXT,
    lot             REAL,
    currency        TEXT,           -- 'UZS' | 'USD'
    base_price      REAL,
    deposit_pct     REAL,
    delivery_basis  TEXT,
    region          TEXT,
    warehouse       TEXT,
    auction_type    TEXT,
    contract_type   TEXT,
    form            TEXT,
    payment_days    INTEGER,
    delivery_days   INTEGER,
    packaging       TEXT,
    seller_inn      TEXT,
    reg_date        TEXT,
    valid_until     TEXT
);

CREATE TABLE participants (
    inn       TEXT PRIMARY KEY,
    name      TEXT,
    region    TEXT,
    activity  TEXT,
    role      TEXT,
    vat_payer TEXT
);

CREATE TABLE brokers (
    broker_code TEXT PRIMARY KEY,
    name        TEXT,
    inn         TEXT,
    region      TEXT
);

CREATE TABLE usd_rates (
    rate_date TEXT PRIMARY KEY,
    rate      REAL
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

INDEXES = """
CREATE INDEX idx_deals_date        ON deals(deal_date);
CREATE INDEX idx_deals_seller      ON deals(seller_inn);
CREATE INDEX idx_deals_buyer       ON deals(buyer_inn);
CREATE INDEX idx_deals_contract    ON deals(contract_number);
CREATE INDEX idx_deals_region      ON deals(region);
CREATE INDEX idx_deals_status      ON deals(status);
CREATE INDEX idx_deals_sbroker     ON deals(seller_broker);
CREATE INDEX idx_deals_bbroker     ON deals(buyer_broker);
CREATE INDEX idx_deals_product     ON deals(product_name);
CREATE INDEX idx_deals_type        ON deals(contract_type);
CREATE INDEX idx_contracts_seller  ON contracts(seller_inn);
CREATE INDEX idx_contracts_product ON contracts(product_name);

DROP TABLE IF EXISTS fts_participants;
DROP TABLE IF EXISTS fts_products;
DROP TABLE IF EXISTS fts_brokers;

CREATE VIRTUAL TABLE fts_participants USING fts5(
    name, inn UNINDEXED, region UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE fts_products USING fts5(
    product_name, contract_number UNINDEXED, category UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE fts_brokers USING fts5(
    name, broker_code UNINDEXED, region UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def currency_code(raw: str | None, contract_type: str | None) -> str:
    if raw and "доллар" in raw.lower():
        return "USD"
    if raw and "сум" in raw.lower():
        return "UZS"
    # Export contracts are settled in USD per dataset README
    return "USD" if (contract_type or "").strip() == "Экспортный" else "UZS"


def build(data_dir: Path, db_path: Path) -> None:
    t0 = time.time()
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    # ---- usd_rates -------------------------------------------------------
    rates: dict[str, float] = {}
    with open(data_dir / "usd_rates.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = date(row["Дата"])
            r = num(row["Курс"])
            if d and r:
                rates[d] = r
    con.executemany("INSERT OR REPLACE INTO usd_rates VALUES (?,?)", rates.items())
    print(f"usd_rates: {len(rates)} rows")

    # ---- participants ----------------------------------------------------
    with open(data_dir / "participants.csv", encoding="utf-8") as f:
        rows = [
            (r["ИНН"].strip(), r["Наименование"].strip(), r["Регион"].strip(),
             r["Вид деятельности"].strip(), r["Роль"].strip(), r["Плательщик НДС"].strip())
            for r in csv.DictReader(f)
        ]
    con.executemany("INSERT OR REPLACE INTO participants VALUES (?,?,?,?,?,?)", rows)
    print(f"participants: {len(rows)} rows")

    # ---- brokers ---------------------------------------------------------
    with open(data_dir / "brokers.csv", encoding="utf-8") as f:
        rows = [
            (r["Код брокера"].strip(), r["Наименование"].strip(),
             r["ИНН"].strip(), r["Регион"].strip())
            for r in csv.DictReader(f)
        ]
    con.executemany("INSERT OR REPLACE INTO brokers VALUES (?,?,?,?)", rows)
    print(f"brokers: {len(rows)} rows")

    # ---- contracts -------------------------------------------------------
    contract_currency: dict[int, str] = {}
    with open(data_dir / "contracts.csv", encoding="utf-8") as f:
        rows = []
        for r in csv.DictReader(f):
            cn = int(num(r["Номер контракта"]) or 0)
            cur = currency_code(r.get("Валюта"), r.get("Тип контракта"))
            contract_currency[cn] = cur
            rows.append((
                cn, r["Наименование продукта"].strip(), r["Марка товара"].strip(),
                r["Размер товара"].strip(), r["Сорт товара"].strip(), r["Тип товара"].strip(),
                r["Класс товара"].strip(), r["Код ТН ВЭД"].strip(), r["Группа товара"].strip(),
                r["Категория товара"].strip(), r["Отрасль промышленности"].strip(),
                r["Единица измерения"].strip(), num(r["Лот"]), cur, num(r["Базисная цена"]),
                num(r["Сумма залога"]), r["Базис поставки"].strip(), r["Регион"].strip(),
                r["Адрес склада"].strip(), r["Тип торгов"].strip(), r["Тип контракта"].strip(),
                r["Форма контракта"].strip(), num(r["Срок оплаты"]), num(r["Срок поставки"]),
                r["Упаковка"].strip(), r["ИНН продавца"].strip(),
                date(r["Дата регистрации"]), date(r["Действует до"]),
            ))
    con.executemany("INSERT OR REPLACE INTO contracts VALUES (" + ",".join(["?"] * 28) + ")", rows)
    print(f"contracts: {len(rows)} rows")

    # ---- deals -----------------------------------------------------------
    total = 0
    insert_sql = "INSERT OR REPLACE INTO deals VALUES (" + ",".join(["?"] * 27) + ")"
    for year in range(2022, 2027):
        path = data_dir / f"deals_{year}.csv"
        if not path.exists():
            continue
        batch = []
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                d = date(r["Дата сделки"])
                cn = int(num(r["Номер контракта"]) or 0)
                amount = num(r["Сумма сделки"])
                qty = num(r["Количество"])
                lot = num(r["Лот"])
                cur = contract_currency.get(cn) or currency_code(None, r.get("Тип контракта"))
                rate = rates.get(d) if cur == "USD" else None
                amount_uzs = None
                if amount is not None:
                    amount_uzs = amount * rate if (cur == "USD" and rate) else amount
                volume = qty * lot if (qty is not None and lot is not None) else None
                unit_price = amount / volume if (amount is not None and volume) else None
                batch.append((
                    int(num(r["Номер сделки"]) or 0), d, r["Время сделки"].strip(),
                    r["Тип торгов"].strip(), qty, amount, cur, rate, amount_uzs,
                    num(r["Стартовая цена"]), cn, r["Наименование товара"].strip(),
                    lot, r["Единица измерения"].strip(), volume, unit_price,
                    r["Регион"].strip(), r["Тип контракта"].strip(), r["Форма контракта"].strip(),
                    r["Состояние сделки"].strip(), r["ИНН продавца"].strip(),
                    r["ИНН покупателя"].strip(), r["Брокер продавца"].strip(),
                    r["Брокер покупателя"].strip(), date(r["Дата оплаты"]),
                    date(r["Дата поставки"]), num(r["Поставлено"]),
                ))
                if len(batch) >= 20_000:
                    con.executemany(insert_sql, batch)
                    total += len(batch)
                    batch = []
        if batch:
            con.executemany(insert_sql, batch)
            total += len(batch)
        print(f"deals_{year}: cumulative {total}")

    # ---- indexes + FTS ----------------------------------------------------
    con.executescript(INDEXES)
    con.execute("INSERT INTO fts_participants(name, inn, region) SELECT name, inn, region FROM participants")
    con.execute("INSERT INTO fts_brokers(name, broker_code, region) SELECT name, broker_code, region FROM brokers")
    # one FTS row per distinct product name (with a representative contract number)
    con.execute("""
        INSERT INTO fts_products(product_name, contract_number, category)
        SELECT product_name, MIN(contract_number), MAX(category)
        FROM contracts GROUP BY product_name
    """)
    con.execute("""
        INSERT OR IGNORE INTO fts_products(product_name, contract_number, category)
        SELECT d.product_name, MIN(d.contract_number), ''
        FROM deals d
        WHERE d.product_name NOT IN (SELECT product_name FROM contracts)
        GROUP BY d.product_name
    """)

    # ---- meta -------------------------------------------------------------
    min_d, max_d, n = con.execute("SELECT MIN(deal_date), MAX(deal_date), COUNT(*) FROM deals").fetchone()
    meta = {
        "min_date": min_d, "max_date": max_d, "deal_count": str(n),
        "participant_count": str(con.execute("SELECT COUNT(*) FROM participants").fetchone()[0]),
        "contract_count": str(con.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]),
        "broker_count": str(con.execute("SELECT COUNT(*) FROM brokers").fetchone()[0]),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cancelled_status": "Аннулирован(Не оплачен)",
    }
    con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    con.commit()
    con.execute("PRAGMA optimize")
    con.close()
    print(f"Done in {time.time() - t0:.1f}s -> {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")
    print(f"Coverage: {min_d} .. {max_d}, {n} deals")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("UZEX_DATA_DIR", "uzex_spot_data"))
    ap.add_argument("--db", default=os.environ.get("UZEX_DB_PATH", "uzex.db"))
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        sys.exit(f"Data dir not found: {data_dir}. Unzip uzex_spot_data.zip first.")
    build(data_dir, Path(args.db))
