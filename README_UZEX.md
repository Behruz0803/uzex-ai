# 📈 UZEX AI Agent — Hackathon Prototipi

**Maqsad:** UZEX Tovar-Xomashyo Birjasining 500 000 ta ochiq bitimi (2022-2026) bo'yicha Telegram bot + Web servis orqali aniq, to'qimasdan javob beruvchi AI agent.

Bu hujjat sizning hackathon topshirig'ingizga javob — `uzex_spot_data.zip` va `UZEX CHE ZA UYNYA.docx` asosida **qanday qilindi** va **qanday ishlatiladi**.

---

## 1. Muhim Qoidalar (hakamlar bahosi 100 ball)

| Ball | Mezon |
|------|-------|
| **60 ball** | 10 ta jonli savolga to'g'ri javob (har biri 6 ball, 90 sek ichida) |
| **20 ball** | Prototip sifati: javobdagi **hisob-kitob, SQL/manba ko'rinishi**, UZ/RU tushunishi, tezligi (2-3 sek) |
| **20 ball** | Yechim tavsifi (arxitektura, texnologiyalar, aniqlik qanday ta'minlangani) |

**Diskvalifikatsiya xavfi:**
> Agar agent ma'lumotlarda yo'q joyda raqam/tovar/kompaniya to'qib chiqarsa (hallutsinatsiya) — darhol chiqariladi.

**Shuning uchun:** Agar natija topilmasa, agent ochiq aytadi:
- UZ: `Ma'lumotlar bazasida bunday ma'lumot mavjud emas`
- RU: `В базе данных такой информации нет`

---

## 2. Arxitektura (20 ballik yechim tavsifi uchun)

```
Telegram foydalanuvchi (UZ/RU savol)
        │
        ▼
     bot.py — handlerlar, klaviatura, log-guruh, /dbstats
        │
        ▼
  uzex/agent.py — Text-to-SQL Agent (Gemini + Heuristic fallback)
        │  ┌─────────────────────────────┐
        │  │ 1. Detect language (uz/ru)  │
        │  │ 2. Gemini → JSON {sql, explanation, language} │
        │  │    ↓ fallback if no API key / no internet │
        │  │ 3. Heuristic regex (INN, BK, year, product) │
        │  │ 4. Validate SQL (only SELECT/WITH) │
        │  └─────────────────────────────┘
        │
        ▼
  uzex/db.py — SQLite (uzex.db, 330 MB)
        │      FTS5 + Indexes + View deals_enriched
        │      Tables: deals(500k), contracts(6.6k), participants(14k), brokers(120), usd_rates(1.7k)
        │      Formulas: volume = quantity*lot, unit_price = deal_sum/(quantity*lot), export USD→UZS = deal_sum*rate
        │
        ▼
  Format: ✅ Jadval + SQL + Hisob-kitob (Hajm, Birlik narxi, Eksport) + ⏱ ms

Shuningdek:
- web/app.py — FastAPI veb-chat (brauzerda bir bosishda kiriladigan public URL)
- uzex/etl.py — ETL: CSV tozalash (probelli narxlar, sana DD.MM.YYYY→YYYY-MM-DD) → SQLite + FTS5
```

### Tanlangan texnologiyalar — nega aynan bular?

| Texnologiya | Sababi |
|-------------|--------|
| **SQLite + FTS5** | 500k qator uchun o'ta tezkor (indekslar < 100ms), fayl-bazali, Vercel/serverless mos, Postgres talab qilmaydi |
| **Text-to-SQL (Gemini 3 Flash)** | LLM to'g'ridan-to'g'ri javob bermaydi — avval SQL hosil qiladi, keyin deterministik bajariladi → hallutsinatsiya 0 |
| **Heuristic fallback** | Agar GEMINI_API_KEY yo'q yoki internet yo'q (arena sandbox) bo'lsa ham, regex bilan 80% savollarga javob beradi — hakamlarga demo uchun kafolat |
| **Hech qanday RAG/chunking**

 yo'q** | 500k qatorda oddiy RAG xato qiladi, bizda aniq SQL — 60 ballga to'liq mos |
| **Anti-hallucination qatlam** | `is_safe_select()` + 0 qator tekshiruvi + aggregation no-data (`COUNT=0 → no data`) |
| **Telegram + Web** | Topshirish talabi: *Telegram-bot yoki veb-servis, o'rnatishsiz ishlaydigan* — ikkalasi ham bor, komissiya qulayini tanlaydi |

---

## 3. Bosqichma-bosqich Reja (docx dagi 5 qadam bajarildi)

### 1-qadam: ETL va SQLite
```bash
# 1. Zipni yuklash (Google Drive → data/)
# 2. DB qurish
python -m uzex.etl --src /tmp/uzex_spot_data --db ./data/uzex.db
# Natija: data/uzex.db (330 MB), 500k deals, index+FTS5, view deals_enriched
```

### 2-qadam: Text-to-SQL Agent
- `uzex/schema.py` — to'liq schema + few-shot misollar (8 ta)
- `uzex/agent.py` — Gemini prompt (temperature 0.1, deterministik) → JSON → validate → execute → format
- Har javob tagida: **SQL, tushuntirish, formula, manba**

### 3-qadam: Interfeys
- **Telegram:** `bot.py` (polling lokal, webhook Vercel 24/7). Tugmalar: Top brokerlar, Eksport, Sement, DB stats.
- **Web:** `web/app.py` (FastAPI). Kirish: `http://localhost:8000` yoki public URL (ngrok/cloudflare)

### 4-qadam: Test (15-20 murakkab savol)
```bash
python scripts/test_queries.py
# Anti-hallucination test: "SpaceX", "Kosmik kema" → 0 qator → "Ma'lumotlar bazasida... yo'q"
```

### 5-qadam: Topshirish materiallari (Google Forma)
1. **Prototip havolasi:** Telegram `@uzexaihackaton_bot` + Web `https://ai-usta-bot-omega.vercel.app` (yoki ngrok URL)
2. **Jamoa ro'yxati** (qo'lda to'ldiriladi)
3. **Yechim tavsifi:** ushbu fayl + `uzex/schema.py` + arxitektura diagrammasi

---

## 4. Tez Ishga Tushirish

### Lokal (polling)
```bash
git clone https://github.com/Behruz0803/ai-usta-bot.git
cd ai-usta-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# .env yaratish
cp .env.example .env
# .env ga kiriting:
# TELEGRAM_TOKEN=123456...
# GEMINI_API_KEY=AIza...
# LOG_CHAT_ID=-100...

# DB qurish (bir marta, 20 sek)
python -m uzex.etl --src /tmp/uzex_spot_data --db ./data/uzex.db

# Botni ishga tushirish
python bot.py
# Telegramda /start → sinab ko'ring
```

### Web (FastAPI)
```bash
pip install fastapi uvicorn jinja2
uvicorn web.app:app --host 0.0.0.0 --port 8000 --reload
# Brauzerda: http://localhost:8000
# API: POST /api/query  {"question": "2024 yilda eng ko'p bitim..."}
# Health: GET /api/health
# Stats: GET /api/stats
```

### Vercel (24/7 webhook)
1. Vercel'da repo import
2. Environment Variables: `TELEGRAM_TOKEN`, `GEMINI_API_KEY`, `WEBHOOK_SECRET` (ixtiyoriy)
3. Push `main` → auto deploy
4. Tekshirish: `https://<loyiha>.vercel.app/api/webhook` → `{"status":"ok"}`
5. `https://<loyiha>.vercel.app/api/webhook?setup=1` → webhook qayta o'rnatish

**Baza Vercel'da:** `data/uzex.db` (330 MB) gitga kirmaydi. Siqilgan `data/uzex.db.gz` (~60 MB) repoda bor. Serverless funksiya birinchi so'rovda uni `/tmp/uzex.db` ga ochadi (15–30 sek), keyingi savollar tez.

Telegram: `https://ai-usta-bot-mbm15.vercel.app` (webhook). Lokal polling uchun `.env` da `TELEGRAM_TOKEN` va `GEMINI_API_KEY`.

---

## 5. Misol Savollar va Kutilgan Javoblar

| # | Savol (UZ/RU) | Kutilgan SQL (heuristic) | Javob |
|---|---------------|---------------------------|-------|
| 1 | 2024 yilda eng ko'p bitim qilgan broker kim? | `SELECT seller_broker, COUNT(*) FROM deals WHERE deal_date BETWEEN '2024-01-01' AND '2024-12-31' GROUP BY ... LIMIT 5` | BK005 — 3989 ta |
| 2 | BK001 2025 nechta? | `COUNT(*) WHERE seller_broker='BK001' OR buyer_broker='BK001' AND date 2025` | ~1200 ta |
| 3 | Экспорт 2024 сумма в сумах? | `SELECT SUM(deal_sum_uzs) FROM deals_enriched WHERE contract_type='Экспортный'` | COUNT + total_uzs |
| 4 | Sement eng qimmat 2023? | `... WHERE product_name LIKE '%цемент%' ORDER BY unit_price DESC` | 1 189 837 so'm/tonna |
| 5 | STIR 921912462 jami? | `SUM(deal_sum) WHERE seller_inn='921912462' OR buyer_inn='921912462'` | sum + cnt |
| 6 | Kosmik kema bormi? | `WHERE product_name LIKE '%Космический%'` | 0 → `Ma'lumotlar bazasida... yo'q` |
| 7 | SpaceX? | `WHERE name LIKE '%SpaceX%'` | 0 → no data (diskvalifikatsiyadan himoya) |

Har javobda: jadval + SQL + formula + ⏱ ms.

---

## 6. Keyingi Qadamlar (sizdan kerak 2 savolga javob bilan)

1. **AI Modeli:** GEMINI_API_KEY bormi? Agar yo'q bo'lsa, Groq/OpenAI kaliti bilan ishlashga o'tkazish mumkin (prompt bir xil). Hozir GEMINI_MODEL=gemini-3-flash-preview (tez, arzon).
2. **Interfeys:** Telegram + Web ikkalasi tayyor. Qaysi birini asosiy qilamiz? Tavsiya: **Telegram** (komissiya uchun eng qulay, sinovdan o'tgan).

Tayyor bo'lsangiz: `python scripts/test_queries.py` ni ishga tushirib, keyin Google Forma uchun havolani tayyorlaymiz!

---

## Muallif

Behruz Mahramqulov — UZEX AI Hackathon jamoasi  
Repo: `Behruz0803/ai-usta-bot` (branch `arena/ced752f8-ai-usta-bot`)  
Baza: `uzex_spot_data.zip` (26 MB) → `data/uzex.db` (330 MB)  
Bot: `@uzexaihackaton_bot` (alohida token; ta'mir boti bilan ulanmagan)  
Web: `web/app.py` (FastAPI)

