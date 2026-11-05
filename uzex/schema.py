# -*- coding: utf-8 -*-
"""
UZEX DB Schema description for Text-to-SQL agent.
Provides human-readable schema for LLM prompt.
"""

SCHEMA_DESCRIPTION = """
# UZEX Spot Market Database (SQLite) — 500 000 сделок, 6 616 контрактов, 14 392 компаний, 120 брокеров

## Tables

### 1. deals — все сделки (500 000 строк) из файлов deals_2022.csv … deals_2026.csv
Columns:
- deal_id INTEGER PRIMARY KEY — Номер сделки (уникальный, сквозной)
- deal_date TEXT (YYYY-MM-DD) — Дата сделки, исходно ДД.ММ.ГГГГ, конвертирована
- deal_time TEXT (HH:MM:SS) — Время сделки
- trade_type TEXT — Тип торгов: 'Английский аукцион' | 'Двусторонний аукцион' | 'Обратный аукцион'
- quantity INTEGER — Количество (сколько лотов куплено)
- deal_sum REAL — Сумма сделки в валюте контракта (очищена от пробела: "304 547 938.4" → 304547938.4)
- start_price REAL — Стартовая цена за единицу в валюте контракта
- contract_id INTEGER — Номер контракта → contracts.contract_id (FK)
- product_name TEXT — Наименование товара (полная строка из реестра, напр. "Шрот хлопковых семян Сорт первый сорт Тип тостированный")
- lot REAL — Лот (сколько единиц в одном лоте), обычно 1, иногда 1000 для топлива
- unit TEXT — Единица измерения: 'килограмм'|'тонна'|'литр'|'метр'|'куб.метр'|'штука'|'кв.метр'
- region TEXT — Регион склада продавца (напр. 'Toshkent viloyati','Farg‘ona viloyati','Qoraqalpog‘iston Respublikasi')
- contract_type TEXT — Тип контракта: 'Внутренний' (сум) | 'Экспортный' (доллар)
- contract_form TEXT — Форма контракта: 'Спот' | 'Форвард' | 'Фьючерс'
- deal_status TEXT — Состояние сделки: 'Исполнен'|'Исполнен(Частично)'|'Оплачен'|'Аннулирован(Не оплачен)'|'Не подписан'|'Заключен'|'Ждёт оплату'|'Новый'
- seller_inn TEXT — ИНН продавца (9 цифр) → participants.inn
- buyer_inn TEXT — ИНН покупателя → participants.inn
- seller_broker TEXT — Брокер продавца (код BK001…) → brokers.broker_code
- buyer_broker TEXT — Брокер покупателя
- payment_date TEXT (YYYY-MM-DD or NULL) — Дата оплаты
- delivery_date TEXT (YYYY-MM-DD or NULL) — Дата поставки
- delivered INTEGER — Поставлено (сколько лотов фактически поставлено, NULL если аннулирована)

Calculated fields (generated columns in DB):
- volume REAL = quantity * lot — Объём в единицах измерения (формула: Количество × Лот)
- unit_price REAL = deal_sum / (quantity * lot) — Цена за единицу (формула: Сумма ÷ (Количество × Лот))

IMPORTANT for Export deals:
- If contracts.currency = 'Доллар США', then deal_sum is in USD. To get sum in UZS: deal_sum * usd_rates.rate
- usd_rates.rate_date must match deals.deal_date

### 2. contracts — карточки контрактов (6 616 строк)
Columns:
- contract_id INTEGER PRIMARY KEY — Номер контракта
- product_name TEXT — Наименование продукта (короткое)
- brand TEXT — Марка товара
- size TEXT — Размер товара
- sort TEXT — Сорт товара
- type TEXT — Тип товара
- class TEXT — Класс товара
- tnved_code TEXT — Код ТН ВЭД
- product_group TEXT — Группа товара
- product_category TEXT — Категория товара
- industry TEXT — Отрасль промышленности
- unit TEXT — Единица измерения (совпадает с deals.unit для этого контракта)
- lot REAL — Лот
- currency TEXT — Валюта: 'Узбекский сум' | 'Доллар США'
- base_price REAL — Базисная цена за единицу на дату регистрации
- deposit_percent TEXT — Сумма залога (напр. '10 %')
- delivery_basis TEXT — Базис поставки
- region TEXT — Регион
- warehouse_address TEXT — Адрес склада
- trade_type TEXT — Тип торгов
- contract_type TEXT — Тип контракта
- contract_form TEXT — Форма контракта
- payment_term INTEGER — Срок оплаты (календарных дней от даты сделки)
- delivery_term INTEGER — Срок поставки (календарных дней от даты оплаты)
- packaging TEXT — Упаковка
- seller_inn TEXT — ИНН продавца → participants.inn
- reg_date TEXT (YYYY-MM-DD) — Дата регистрации
- valid_until TEXT (YYYY-MM-DD) — Действует до

### 3. participants — компании (14 392 строк)
Columns:
- inn TEXT PRIMARY KEY — ИНН (9 цифр, строковый!)
- name TEXT — Наименование (напр. '"ҲАРАЙЎМ КИМЁ" МЧЖ', 'KAHIR BROKER MCHJ')
- region TEXT — Регион
- activity_type TEXT — Вид деятельности: 'Производитель'|'Торговая компания'|'...' 
- role TEXT — Роль: 'Продавец'|'Покупатель'
- vat_payer TEXT — Плательщик НДС: 'Да'|'Нет'

### 4. brokers — брокерские конторы (120 строк)
Columns:
- broker_code TEXT PRIMARY KEY — Код брокера (BK001…BK120)
- name TEXT — Наименование
- inn TEXT — ИНН брокера
- region TEXT — Регион

### 5. usd_rates — курс доллара (1 766 строк, каждый календарный день 01.01.2022 – 31.10.2026)
Columns:
- rate_date TEXT PRIMARY KEY — Дата (YYYY-MM-DD)
- rate REAL — Курс, сум за 1 USD (напр. 10839.03)

### 6. View: deals_enriched — удобное представление с уже посчитанной суммой в сумах
CREATE VIEW deals_enriched AS
SELECT d.*,
       c.currency,
       c.product_category,
       c.product_group,
       c.industry,
       CASE WHEN c.currency = 'Доллар США' THEN d.deal_sum * r.rate ELSE d.deal_sum END AS deal_sum_uzs,
       r.rate AS usd_rate
FROM deals d
LEFT JOIN contracts c ON d.contract_id = c.contract_id
LEFT JOIN usd_rates r ON d.deal_date = r.rate_date;

Use deals_enriched when question asks for sum in UZS / сўмда / в сумах, especially for Экспортный deals.

## Indexes & FTS
- Indexes on: deal_date, seller_inn, buyer_inn, contract_id, product_name, region, deal_status, seller_broker, buyer_broker
- FTS5 tables:
  * participants_fts(inn, name) — for company search by ИНН or name fragment
  * brokers_fts(broker_code, name)
  * deals_fts(product_name) — for product search
  * contracts_fts(product_name, brand, product_category)

## Important Notes for SQL Generation
- NEVER invent INN, company name, product name, broker code — only use LIKE / FTS that will return 0 rows if not exists.
- Dates: user may say "2024 йил", "2024-yilda", "в 2024 году" → translate to WHERE deal_date BETWEEN '2024-01-01' AND '2024-12-31' or strftime('%Y', deal_date)='2024'
- Regions may be in Uzbek Latin (viloyati) or Russian (область) — use LIKE '%Toshkent%' or '%Ташкент%'
- Product names are dirty: same product written differently ("1 сорт" vs "1-сорт", with/without "Марка", upper/lower). COLLATE NOCASE does NOT fold Cyrillic. Use ufold(product_name) LIKE ufold('%цемент%'). Paxta yog'i is 'Хлопковое масло' OR 'Масло хлопковое', never '%пахта%йог%'.
- Names and regions use Unicode apostrophe ‘ (U+2018): Farg‘ona, Mo‘jotat. Search with '%Farg%ona%' / '%Mo%jotat%', not ASCII-only apostrophe.
- Quantity * Lot = volume, Sum / (Quantity*Lot) = unit price — show formula in answer footer.
- For "самый дорогой / eng qimmat" use ORDER BY unit_price DESC LIMIT 1, for "самый дешевый" ASC.
- For aggregations: COUNT, SUM, AVG, MIN, MAX, GROUP BY. Always handle NULL payment_date/delivery_date (Аннулирован deals).
- For Export deals sum in UZS: JOIN usd_rates and contracts to multiply.
- If question asks about seller/buyer by INN, filter by seller_inn = 'XXXXXXXXX' (as TEXT, keep leading zeros if any, though all are 9-digit without zero prefix) — use TEXT comparison.
- For broker stats: GROUP BY seller_broker or buyer_broker.
- Always LIMIT 100 unless aggregation, to avoid huge output.
"""

# SQL sanity checks
ALLOWED_SQL_PREFIXES = ("select", "with")

# Example few-shot SQL patterns for LLM prompt
FEW_SHOT_EXAMPLES = """
### Примеры правильных SQL:

1. Вопрос (UZ): "2024 йилда STIR 123456789 бўлган корхона жами қанча харид қилган?"
   SQL: SELECT SUM(d.deal_sum) AS total FROM deals d WHERE d.buyer_inn = '123456789' AND d.deal_date BETWEEN '2024-01-01' AND '2024-12-31' AND d.deal_status != 'Аннулирован(Не оплачен)';

2. Вопрос (RU): "Какой брокер провел больше всего сделок в 2025 году?"
   SQL: SELECT b.name, b.broker_code, COUNT(*) AS total_deals FROM deals d JOIN brokers b ON b.broker_code = d.seller_broker OR b.broker_code = d.buyer_broker WHERE d.deal_date BETWEEN '2025-01-01' AND '2025-12-31' GROUP BY b.broker_code, b.name ORDER BY total_deals DESC LIMIT 1;
   Не используй UNION ALL — если брокер и продавец, и покупатель в одной сделке, UNION ALL посчитает её дважды. OR в JOIN считает сделку один раз.

3. Вопрос (RU): "Самая дорогая сделка по цементу в 2023 году — цена за тонну?"
   SQL: SELECT deal_id, deal_date, product_name, (deal_sum / (quantity * lot)) AS unit_price, deal_sum, quantity, lot, unit FROM deals WHERE product_name LIKE '%цемент%' COLLATE NOCASE AND deal_date BETWEEN '2023-01-01' AND '2023-12-31' ORDER BY unit_price DESC LIMIT 5;

4. Вопрос (UZ): "Paxta yog'i bo'yicha 2025 yilda faqat ijro etilgan, tonna hisobida eng arzon VA eng qimmat bitim?"
   SQL: SELECT deal_id, deal_date, product_name, unit, deal_status, ROUND(deal_sum*1.0/(quantity*lot),2) AS unit_price FROM deals WHERE (ufold(product_name) LIKE ufold('%хлопковое масло%') OR ufold(product_name) LIKE ufold('%масло хлопковое%')) AND ufold(product_name) NOT LIKE ufold('%подсолнеч%') AND ufold(product_name) NOT LIKE ufold('%шрот%') AND unit='тонна' AND deal_status='Исполнен' AND deal_date BETWEEN '2025-01-01' AND '2025-12-31' AND quantity>0 AND lot>0 ORDER BY unit_price ASC LIMIT 1;
   Затем тот же запрос ORDER BY unit_price DESC LIMIT 1. Имена бывают в двух порядках: 'Хлопковое масло' и 'Масло хлопковое'. '%пахта%йог%' НЕ использовать.

5. Вопрос (RU): "Сколько экспортных сделок было в 2024 году и их сумма в сумах?"
   SQL: SELECT COUNT(*) AS cnt, SUM(de.deal_sum_uzs) AS total_uzs FROM deals_enriched de WHERE de.contract_type='Экспортный' AND de.deal_date BETWEEN '2024-01-01' AND '2024-12-31';

6. Вопрос (UZ): "BK001 brokeri qancha bitimda ishtirok etgan?"
   SQL: SELECT COUNT(*) AS cnt FROM deals WHERE seller_broker='BK001' OR buyer_broker='BK001';

7. Anti-hallucination: Если продукт не найден, запрос вернет 0 строк — это нормально. Не выдумывай данные.
   Пример несуществующего товара: "Космический корабль" → SELECT ... WHERE product_name LIKE '%космический корабль%' → 0 rows → ответ "Ma'lumotlar bazasida bunday ma'lumot mavjud emas"

8. Export sum: "Экспортная сделка 280091 сумма в сумах?"
   SQL: SELECT d.deal_id, d.deal_sum, c.currency, r.rate, (d.deal_sum * r.rate) AS sum_uzs FROM deals d JOIN contracts c ON d.contract_id=c.contract_id JOIN usd_rates r ON d.deal_date=r.rate_date WHERE d.deal_id=280091;
"""
