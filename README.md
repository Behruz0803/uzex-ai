# UZEX AI — Birja bitimlari bo'yicha AI-tahlilchi / AI-аналитик биржевых сделок UZEX

**UZEX AI Hackathon 2026 — saralash bosqichi prototipi.**
Telegram-bot va ochiq veb-interfeys orqali O'zbekiston respublika tovar-xom ashyo
birjasining (UZEX) **500 000 ta bitimi** (2022–2026) bo'yicha aniq, manbaga
asoslangan javoblar beradi. Har bir javob ostida **SQL so'rov** ko'rsatiladi.

> ⚠️ Ma'lumotlar to'plami o'ylab topilgan (hackathon uchun), format real ochiq
> bitimlar reyestrini takrorlaydi.

---

## ✨ Asosiy imkoniyatlar

- **Ikki til** — o'zbek (lotin) va rus tillarida savol-javob. Til avtomatik
  aniqlanadi; qisqa/noaniq so'rovlar (masalan `BK001`) sukut bo'yicha **o'zbekcha**
  javob oladi (hech qachon ruscha emas).
- **Aniq, manbaga asoslangan javoblar** — Text-to-SQL + SQLite. Agent mavjud
  bo'lmagan faktni **o'ylab topmaydi**: natija bo'sh bo'lsa, aniq "ma'lumot yo'q"
  deydi va bazaning qamrov oralig'ini (2022-01-03 — 2026-10-30) ko'rsatadi.
- **Yaqin natijalar (near-match)** — agar aynan shartlarga mos ma'lumot topilmasa,
  lekin shartlar kengaytirilganda yaqin natijalar bo'lsa, agent buni **ochiq
  aytadi**:
  > «Aynan sizning shartlaringizga to'g'ri keladigan ma'lumot topilmadi, lekin
  > shunga yaqin bir nechta natija bor: …»
  > «Данных, точно соответствующих вашим условиям, не найдено, но есть несколько
  > близких результатов: …»
- **Shaffoflik** — har bir javob tagida bajarilgan SQL so'rov (hakamlar uchun
  hisob-kitob/manba ko'rinishi).
- **Barqarorlik** — Gemini API 503/429 xatolarida retry + exponential backoff +
  model fallback; API butunlay ishlamasa yoki kalit bo'lmasa, **mahalliy
  deterministik dvigatel** aniq javob berishda davom etadi.

---

## 🏗 Arxitektura

```
Foydalanuvchi (Telegram / Veb)
        │  savol (UZ / RU)
        ▼
┌─────────────────────────────────────────────────────────┐
│ agent.py — UzexAgent                                     │
│  1. Til aniqlash (detect_language)                       │
│  2. Off-topic / gibberish / injection himoyasi           │
│  3. Entity qidiruv (db_helper.search_entities)           │
│       → FTS5: participants + products + brokers          │
│       → regex: STIR, BKxxx, shartnoma 10xxxx, bitim №     │
│  4. Text-to-SQL (Gemini, retry/backoff/fallback)         │
│       yoki mahalliy dvigatel (fallback_engine)           │
│  5. Xavfsiz SQL: faqat bitta SELECT, read-only, timeout  │
│  6. Self-correction (xato bo'lsa LLM tuzatadi)           │
│  7. Bo'sh natija → near-match relaxation                 │
│  8. Javob sintezi (faqat UZ/RU, faqat natija asosida)    │
└─────────────────────────────────────────────────────────┘
        │
        ▼
   uzex.db (SQLite + FTS5 + indekslar, 500 000 bitim)
```

**Texnologiyalar:** Python 3.11, SQLite (FTS5), Google Gemini (`google-genai`),
FastAPI + Uvicorn (veb), `python-telegram-bot` (Telegram), deterministik
Text-to-SQL fallback.

### Nima uchun Text-to-SQL?
500 ming qatorda oddiy RAG/prompt LLM adashadi va gallyutsinatsiya qiladi
(hakamlar qoidasi bo'yicha diskvalifikatsiya). Deterministik SQL + qat'iy
"natija bo'sh → ma'lumot yo'q" filtri aniqlikni kafolatlaydi.

---

## 🔧 Audit kamchiliklari va ularning yechimlari (05.11.2026)

| # | Kamchilik | Yechim | Fayl |
|---|-----------|--------|------|
| 1 | Gemini `503 UNAVAILABLE` / rate-limit, retry yo'q | Retry + exponential backoff + model fallback (`gemini-3.8-flash` olib tashlandi); API ishlamasa mahalliy dvigatel | `agent.py` (`LLM`), `fallback_engine.py` |
| 2 | O'zbekcha savolga ruscha javob (14 so'zli til aniqlash) | Kirill/lotin + kengaytirilgan lug'at bo'yicha ball; sukut bo'yicha **uz**; foydalanuvchi til xotirasi | `agent.py` (`detect_language`), `bot.py` |
| 3 | Ma'nosiz so'rovda gallyutsinatsiya, ispancha javob, injection | Off-topic guard + `NO_QUERY` qoidasi; faqat bitta read-only `SELECT`; javob tili majburan UZ/RU | `agent.py`, `db_helper.py` (`validate_sql`) |
| 4 | Telegram 4096 belgi limiti → `Message is too long` | 4000 belgidan bo'lib yuborish (chunking), Markdown→plain fallback | `bot.py` (`split_message`, `send_split_message`) |
| 5 | FTS faqat `fts_participants`; bitim raqami regexi yo'q | `fts_participants` + `fts_products` + `fts_brokers`; STIR/BK/shartnoma/**bitim** raqamlari regexi | `db_helper.py` (`search_entities`), `etl.py` |
| 6 | Bot serverda o'chiq (offline) | Ishga tushirish/deploy yo'riqnomasi + o'z-o'zidan ishlaydigan veb prototip | `README`, `Dockerfile`, `Procfile` |
| ★ | Aniq moslik yo'qda yaqin javob so'rash | Near-match relaxation + aniq ogohlantirish matni (UZ/RU) | `agent.py`, `fallback_engine.py` |

---

## 🚀 Ishga tushirish

### 1. Talablar
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Ma'lumotlar bazasini qurish
`uzex_spot_data.zip` ni oching (`uzex_spot_data/` papkasi hosil bo'ladi), so'ng:
```bash
python etl.py            # -> uzex.db (~300 MB, 500 000 bitim, FTS5)
```

### 3. Sozlash
`.env.example` dan `.env` yarating:
```ini
GEMINI_API_KEY=...          # ixtiyoriy: bo'lmasa mahalliy dvigatel ishlaydi
TELEGRAM_BOT_TOKEN=...      # Telegram bot uchun (@BotFather)
```

### 4a. Veb prototip (hakamlar uchun ochiq havola)
```bash
python -m webapp.server     # http://0.0.0.0:8000
```
Ro'yxatdan o'tish/o'rnatish talab qilinmaydi. `/api/health`, `/api/examples`,
`/api/ask` endpointlari mavjud.

### 4b. Telegram bot
```bash
python bot.py               # polling; /start, /help, /til uz|ru
```

### Testlar
```bash
python tests/test_fixes.py    # 26 test: audit tuzatishlari + near-match
python tests/test_gemini.py   # 11 test: Gemini retry/fallback (mock, kalitsiz)
```

---

## ☁️ Deploy (ommaviy havola)

**Render / Railway:** `Procfile` tayyor (`web: uvicorn webapp.server:app ...`).
`uzex.db` ni repoga qo'shmang — deploy paytida `python etl.py` ni build qadamida
ishga tushiring yoki tayyor `uzex.db` ni persistent diskka yuklang.

**Docker:**
```bash
docker build -t uzex-ai .
docker run -p 8000:8000 -e GEMINI_API_KEY=... uzex-ai
```

**Telegram botni doimiy ishlatish:** polling uchun oddiy VPS/systemd yoki
Railway worker yetarli. Bot offline bo'lmasligi uchun jarayonni kuzatuvchi
(systemd `Restart=always`, yoki platforma health-check) ishlating.

---

## ✅ Hakamlar tekshiruv ro'yxati (5 band)

1. **Havola ishlayaptimi?** — veb `0.0.0.0` da, boshqa qurilmadan ochiladi.
2. **Kirish ochiqmi?** — ro'yxatdan o'tish/o'rnatish yo'q.
3. **Ikki tilda ishlayaptimi?** — UZ/RU avtomatik + `/til` va veb til tugmalari.
4. **Javoblar aniqmi?** — SQL + read-only + bo'sh natijada "ma'lumot yo'q";
   gallyutsinatsiya yo'q; yaqin natijalar aniq belgilanadi.
5. **Forma yuborildimi?** — prototip havolasini Google Formaga kiritib, formani
   **yuborishni** unutmang.

---

## 📁 Fayllar tuzilishi

```
etl.py               CSV -> uzex.db (ETL, FTS5, indekslar)
db_helper.py         Xavfsiz SQL, entity/FTS qidiruv, sxema
agent.py             UzexAgent: til, guard, Gemini, near-match, javob
fallback_engine.py   Deterministik mahalliy Text-to-SQL + formatlash
bot.py               Telegram bot (chunking, til xotirasi)
webapp/server.py     FastAPI veb prototip
webapp/static/       Chat UI (UZ/RU, SQL ko'rsatish)
tests/               Regressiya testlari
```

## 📊 Ma'lumotlar manbasi
`uzex_spot_data/` — `deals_2022..2026.csv` (500 000 bitim), `contracts.csv`
(6 616), `participants.csv` (14 392), `brokers.csv` (120), `usd_rates.csv`.
Eksport bitimlari so'mga `usd_rates.csv` kursi bo'yicha konvertatsiya qilingan
(`amount_uzs`). Hajm = Miqdor × Lot; birlik narxi = Summa ÷ Hajm.
