# -*- coding: utf-8 -*-
"""
ETL: Build SQLite DB uzex.db from CSVs ( узбек/рус mix )
- Cleans numeric fields with spaces
- Converts dates DD.MM.YYYY → YYYY-MM-DD
- Creates indexes + FTS5
- Creates view deals_enriched for UZS conversion

Usage:
  python -m uzex.etl --src /tmp/uzex_spot_data --db ./data/uzex.db
  or python uzex/etl.py
"""
import argparse
import csv
import sqlite3
import pathlib
import re
import sys
from datetime import datetime

# CSV files expected in src directory
FILES = {
    "contracts": "contracts.csv",
    "brokers": "brokers.csv",
    "participants": "participants.csv",
    "usd_rates": "usd_rates.csv",
}

def clean_numeric(val: str):
    """Clean '304 547 938.4' / '3 731.07' -> float, handle empty, spaces, nbsp"""
    if val is None or val.strip() == "" or val.strip() == "-":
        return None
    # remove spaces, nbsp, thin spaces
    s = val.replace(" ", "").replace("\xa0", "").replace("\u202f", "").replace(",", "")
    # some values like "10 %" keep as string, handle separately
    try:
        return float(s)
    except:
        return None

def clean_int(val: str):
    if val is None or val.strip() == "":
        return None
    s = val.replace(" ", "").replace("\xa0", "")
    try:
        return int(float(s))
    except:
        return None

def parse_date(d: str):
    """DD.MM.YYYY -> YYYY-MM-DD, empty -> None"""
    if not d or d.strip() == "":
        return None
    d = d.strip()
    try:
        dt = datetime.strptime(d, "%d.%m.%Y")
        return dt.strftime("%Y-%m-%d")
    except:
        return None

def strip_all(s):
    return s.strip() if isinstance(s, str) else s

def build_db(src_dir: pathlib.Path, db_path: pathlib.Path):
    src_dir = pathlib.Path(src_dir)
    db_path = pathlib.Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # remove existing DB
    if db_path.exists():
        print(f"Removing existing DB {db_path}")
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # Increase cache
    conn.execute("PRAGMA cache_size=-64000;")
    conn.execute("PRAGMA temp_store=MEMORY;")

    cur = conn.cursor()

    # === Create tables ===
    print("Creating tables...")
    cur.execute("""
    CREATE TABLE participants (
        inn TEXT PRIMARY KEY,
        name TEXT,
        region TEXT,
        activity_type TEXT,
        role TEXT,
        vat_payer TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE brokers (
        broker_code TEXT PRIMARY KEY,
        name TEXT,
        inn TEXT,
        region TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE contracts (
        contract_id INTEGER PRIMARY KEY,
        product_name TEXT,
        brand TEXT,
        size TEXT,
        sort TEXT,
        type TEXT,
        class TEXT,
        tnved_code TEXT,
        product_group TEXT,
        product_category TEXT,
        industry TEXT,
        unit TEXT,
        lot REAL,
        currency TEXT,
        base_price REAL,
        deposit_percent TEXT,
        delivery_basis TEXT,
        region TEXT,
        warehouse_address TEXT,
        trade_type TEXT,
        contract_type TEXT,
        contract_form TEXT,
        payment_term INTEGER,
        delivery_term INTEGER,
        packaging TEXT,
        seller_inn TEXT,
        reg_date TEXT,
        valid_until TEXT,
        FOREIGN KEY(seller_inn) REFERENCES participants(inn)
    );
    """)
    cur.execute("""
    CREATE TABLE usd_rates (
        rate_date TEXT PRIMARY KEY,
        rate REAL
    );
    """)
    cur.execute("""
    CREATE TABLE deals (
        deal_id INTEGER PRIMARY KEY,
        deal_date TEXT,
        deal_time TEXT,
        trade_type TEXT,
        quantity INTEGER,
        deal_sum REAL,
        start_price REAL,
        contract_id INTEGER,
        product_name TEXT,
        lot REAL,
        unit TEXT,
        region TEXT,
        contract_type TEXT,
        contract_form TEXT,
        deal_status TEXT,
        seller_inn TEXT,
        buyer_inn TEXT,
        seller_broker TEXT,
        buyer_broker TEXT,
        payment_date TEXT,
        delivery_date TEXT,
        delivered INTEGER,
        volume REAL GENERATED ALWAYS AS (quantity * lot) STORED,
        unit_price REAL GENERATED ALWAYS AS (deal_sum / (quantity * lot)) STORED,
        FOREIGN KEY(contract_id) REFERENCES contracts(contract_id),
        FOREIGN KEY(seller_inn) REFERENCES participants(inn),
        FOREIGN KEY(buyer_inn) REFERENCES participants(inn),
        FOREIGN KEY(seller_broker) REFERENCES brokers(broker_code),
        FOREIGN KEY(buyer_broker) REFERENCES brokers(broker_code)
    );
    """)

    # === Load participants ===
    print("Loading participants...")
    part_file = src_dir / FILES["participants"]
    with open(part_file, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            rows.append((
                r["ИНН"].strip(),
                r["Наименование"],
                r["Регион"],
                r["Вид деятельности"],
                r["Роль"],
                r["Плательщик НДС"],
            ))
        cur.executemany("INSERT INTO participants VALUES (?,?,?,?,?,?)", rows)
        print(f"  participants: {len(rows)}")

    # === Load brokers ===
    print("Loading brokers...")
    brok_file = src_dir / FILES["brokers"]
    with open(brok_file, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            rows.append((
                r["Код брокера"].strip(),
                r["Наименование"],
                r["ИНН"].strip(),
                r["Регион"],
            ))
        cur.executemany("INSERT INTO brokers VALUES (?,?,?,?)", rows)
        print(f"  brokers: {len(rows)}")

    # === Load usd_rates ===
    print("Loading usd_rates...")
    usd_file = src_dir / FILES["usd_rates"]
    with open(usd_file, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            rows.append((parse_date(r["Дата"]), clean_numeric(r["Курс"])))
        cur.executemany("INSERT INTO usd_rates VALUES (?,?)", rows)
        print(f"  usd_rates: {len(rows)}")

    # === Load contracts ===
    print("Loading contracts...")
    cont_file = src_dir / FILES["contracts"]
    with open(cont_file, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            rows.append((
                clean_int(r["Номер контракта"]),
                r["Наименование продукта"],
                r["Марка товара"],
                r["Размер товара"],
                r["Сорт товара"],
                r["Тип товара"],
                r["Класс товара"],
                r["Код ТН ВЭД"],
                r["Группа товара"],
                r["Категория товара"],
                r["Отрасль промышленности"],
                r["Единица измерения"],
                clean_numeric(r["Лот"]),
                r["Валюта"],
                clean_numeric(r["Базисная цена"]),
                r["Сумма залога"],
                r["Базис поставки"],
                r["Регион"],
                r["Адрес склада"],
                r["Тип торгов"],
                r["Тип контракта"],
                r["Форма контракта"],
                clean_int(r["Срок оплаты"]),
                clean_int(r["Срок поставки"]),
                r["Упаковка"],
                r["ИНН продавца"].strip(),
                parse_date(r["Дата регистрации"]),
                parse_date(r["Действует до"]),
            ))
        cur.executemany("""
            INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
        print(f"  contracts: {len(rows)}")

    # === Load deals (multiple files) ===
    print("Loading deals...")
    deal_files = sorted(src_dir.glob("deals_*.csv"))
    if not deal_files:
        print("No deals files found!", file=sys.stderr)
        sys.exit(1)
    total = 0
    for df in deal_files:
        print(f"  reading {df.name}...")
        with open(df, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            batch = []
            for r in reader:
                # Clean numeric
                deal_id = clean_int(r["Номер сделки"])
                quantity = clean_int(r["Количество"])
                deal_sum = clean_numeric(r["Сумма сделки"])
                start_price = clean_numeric(r["Стартовая цена"])
                contract_id = clean_int(r["Номер контракта"])
                lot = clean_numeric(r["Лот"])
                delivered = clean_int(r["Поставлено"]) if r["Поставлено"].strip() else None
                # Dates
                deal_date = parse_date(r["Дата сделки"])
                payment_date = parse_date(r["Дата оплаты"])
                delivery_date = parse_date(r["Дата поставки"])

                batch.append((
                    deal_id,
                    deal_date,
                    r["Время сделки"],
                    r["Тип торгов"],
                    quantity,
                    deal_sum,
                    start_price,
                    contract_id,
                    r["Наименование товара"],
                    lot,
                    r["Единица измерения"],
                    r["Регион"],
                    r["Тип контракта"],
                    r["Форма контракта"],
                    r["Состояние сделки"],
                    r["ИНН продавца"].strip(),
                    r["ИНН покупателя"].strip(),
                    r["Брокер продавца"].strip(),
                    r["Брокер покупателя"].strip(),
                    payment_date,
                    delivery_date,
                    delivered,
                ))
                if len(batch) >= 5000:
                    cur.executemany("""
                        INSERT INTO deals (deal_id, deal_date, deal_time, trade_type, quantity, deal_sum, start_price, contract_id, product_name, lot, unit, region, contract_type, contract_form, deal_status, seller_inn, buyer_inn, seller_broker, buyer_broker, payment_date, delivery_date, delivered)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, batch)
                    total += len(batch)
                    batch = []
            if batch:
                cur.executemany("""
                    INSERT INTO deals (deal_id, deal_date, deal_time, trade_type, quantity, deal_sum, start_price, contract_id, product_name, lot, unit, region, contract_type, contract_form, deal_status, seller_inn, buyer_inn, seller_broker, buyer_broker, payment_date, delivery_date, delivered)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, batch)
                total += len(batch)
        conn.commit()
        print(f"    total so far: {total}")
    print(f"  deals total: {total}")

    conn.commit()

    # === Create indexes ===
    print("Creating indexes...")
    indexes = [
        "CREATE INDEX idx_deals_date ON deals(deal_date);",
        "CREATE INDEX idx_deals_seller_inn ON deals(seller_inn);",
        "CREATE INDEX idx_deals_buyer_inn ON deals(buyer_inn);",
        "CREATE INDEX idx_deals_contract ON deals(contract_id);",
        "CREATE INDEX idx_deals_product ON deals(product_name);",
        "CREATE INDEX idx_deals_region ON deals(region);",
        "CREATE INDEX idx_deals_status ON deals(deal_status);",
        "CREATE INDEX idx_deals_seller_broker ON deals(seller_broker);",
        "CREATE INDEX idx_deals_buyer_broker ON deals(buyer_broker);",
        "CREATE INDEX idx_deals_contract_type ON deals(contract_type);",
        "CREATE INDEX idx_deals_contract_form ON deals(contract_form);",
        "CREATE INDEX idx_deals_trade_type ON deals(trade_type);",
        "CREATE INDEX idx_deals_unit_price ON deals(unit_price);",
        "CREATE INDEX idx_contracts_product ON contracts(product_name);",
        "CREATE INDEX idx_contracts_category ON contracts(product_category);",
        "CREATE INDEX idx_contracts_currency ON contracts(currency);",
        "CREATE INDEX idx_contracts_seller_inn ON contracts(seller_inn);",
        "CREATE INDEX idx_participants_name ON participants(name);",
        "CREATE INDEX idx_participants_region ON participants(region);",
        "CREATE INDEX idx_brokers_region ON brokers(region);",
        "CREATE INDEX idx_usd_rates_date ON usd_rates(rate_date);",
    ]
    for idx in indexes:
        cur.execute(idx)
    conn.commit()
    print("  indexes done")

    # === Create FTS5 ===
    print("Creating FTS5...")
    # participants_fts
    cur.execute("""
        CREATE VIRTUAL TABLE participants_fts USING fts5(
            inn,
            name,
            content='participants',
            content_rowid='rowid'
        );
    """)
    cur.execute("INSERT INTO participants_fts(rowid, inn, name) SELECT rowid, inn, name FROM participants;")

    cur.execute("""
        CREATE VIRTUAL TABLE brokers_fts USING fts5(
            broker_code,
            name,
            content='brokers',
            content_rowid='rowid'
        );
    """)
    cur.execute("INSERT INTO brokers_fts(rowid, broker_code, name) SELECT rowid, broker_code, name FROM brokers;")

    cur.execute("""
        CREATE VIRTUAL TABLE deals_fts USING fts5(
            product_name,
            content='deals',
            content_rowid='rowid'
        );
    """)
    cur.execute("INSERT INTO deals_fts(rowid, product_name) SELECT rowid, product_name FROM deals;")

    cur.execute("""
        CREATE VIRTUAL TABLE contracts_fts USING fts5(
            product_name,
            brand,
            product_category,
            content='contracts',
            content_rowid='rowid'
        );
    """)
    cur.execute("INSERT INTO contracts_fts(rowid, product_name, brand, product_category) SELECT rowid, product_name, brand, product_category FROM contracts;")

    conn.commit()
    print("  FTS done")

    # === Create view deals_enriched ===
    print("Creating view deals_enriched...")
    cur.execute("""
        CREATE VIEW deals_enriched AS
        SELECT 
            d.deal_id,
            d.deal_date,
            d.deal_time,
            d.trade_type,
            d.quantity,
            d.deal_sum,
            d.start_price,
            d.contract_id,
            d.product_name,
            d.lot,
            d.unit,
            d.region,
            d.contract_type,
            d.contract_form,
            d.deal_status,
            d.seller_inn,
            d.buyer_inn,
            d.seller_broker,
            d.buyer_broker,
            d.payment_date,
            d.delivery_date,
            d.delivered,
            d.volume,
            d.unit_price,
            c.currency,
            c.product_group,
            c.product_category,
            c.industry,
            r.rate AS usd_rate,
            CASE WHEN c.currency = 'Доллар США' THEN d.deal_sum * r.rate ELSE d.deal_sum END AS deal_sum_uzs,
            CASE WHEN c.currency = 'Доллар США' THEN (d.deal_sum / (d.quantity * d.lot)) * r.rate ELSE (d.deal_sum / (d.quantity * d.lot)) END AS unit_price_uzs
        FROM deals d
        LEFT JOIN contracts c ON d.contract_id = c.contract_id
        LEFT JOIN usd_rates r ON d.deal_date = r.rate_date;
    """)
    conn.commit()
    print("  view done")

    # === Analyze ===
    print("Running ANALYZE...")
    cur.execute("ANALYZE;")
    conn.commit()

    # === Stats ===
    cur.execute("SELECT count(*) FROM deals;")
    print("deals:", cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM contracts;")
    print("contracts:", cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM participants;")
    print("participants:", cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM brokers;")
    print("brokers:", cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM usd_rates;")
    print("usd_rates:", cur.fetchone()[0])

    conn.close()
    print(f"\n✅ DB built at {db_path} (size: {db_path.stat().st_size / 1024 / 1024:.1f} MB)")

def main():
    parser = argparse.ArgumentParser(description="Build UZEX SQLite DB")
    parser.add_argument("--src", default="/tmp/uzex_spot_data", help="CSV source dir")
    parser.add_argument("--db", default="./data/uzex.db", help="SQLite output path")
    args = parser.parse_args()
    build_db(args.src, args.db)

if __name__ == "__main__":
    main()
