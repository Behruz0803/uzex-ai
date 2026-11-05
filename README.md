# 📈 UZEX AI — Birja bitimlari bo'yicha AI-agent

**UZEX AI Hackathon 2026 prototipi.** O'zbekiston respublika tovar-xom ashyo
birjasining **500 000 ta bitimi** (2022–2026) bo'yicha Telegram-bot va ochiq veb
servis orqali aniq, manbaga asoslangan javoblar. Har bir javob ostida **SQL
so'rov + hisob-kitob** ko'rsatiladi. O'zbek va rus tillarida ishlaydi.

> Bu repo `ai-usta-bot` loyihasidagi real UZEX agent kodini (`uzex/` paketi,
> `bot.py`, `api/webhook.py`, `web/app.py`) va unga kiritilgan audit
> tuzatishlarini o'z ichiga oladi. Batafsil yechim tavsifi: [README_UZEX.md](README_UZEX.md).

---

## 🔧 05.11.2026 audit kamchiliklari — tuzatildi

Ikki docx hisobot (`UZEX_AI_Bot_Audit_va_Test_Hisoboti.docx`, `prablemniy.docx`)
dagi kamchiliklar real kodga kiritildi:

| # | Kamchilik | Yechim | Fayl |
|---|-----------|--------|------|
| 1 | Gemini **503 UNAVAILABLE / rate-limit**, retry yo'q | Retry raundlari + **exponential backoff + jitter**, model fallback zanjiri; faqat auth xatolari erta to'xtaydi (503/429 endi qayta uriniladi) | `uzex/agent.py` |
| 2 | O'zbekcha savolga **ruscha javob** | Kengaytirilgan UZ/RU markerlar, `o'/g'` apostrof boost, noaniq qisqa so'rov (`BK001`) sukut bo'yicha **UZ** | `uzex/agent.py` (`detect_language`) |
| 3 | Ma'nosiz so'rovda **gallyutsinatsiya**, injection | `needs_data` guard + `is_safe_select` (faqat bitta read-only SELECT/WITH) | `uzex/agent.py`, `uzex/db.py` |
| 4 | Telegram **4096 belgi** limiti | 4000 belgidan bo'lib yuborish (chunking) + HTML/Markdown fallback | `bot.py` (`xavfsiz_reply`) |
| 5 | FTS faqat participants; bitim raqami yo'q | `search_entities()` **barcha** FTS jadvallarini qidiradi (participants, brokers, deals, contracts) + STIR/BK/shartnoma/**bitim** raqamlari + UZ→RU mahsulot sinonimlari; Gemini promptiga grounding sifatida uzatiladi | `uzex/db.py` |
| 6 | Bot **offline** | Vercel webhook + polling + deploy yo'riqnomasi | `api/webhook.py`, `scripts/`, README |

### ★ Yangi imkoniyat — yaqin javoblar (near-match)

Agar **aynan** shartlarga mos ma'lumot topilmasa, lekin shartlar kengaytirilganda
yaqin natijalar bo'lsa, agent buni **ochiq aytadi** (o'ylab topmaydi):

- **UZ:** «Aynan sizning shartlaringizga to'g'ri keladigan ma'lumot topilmadi,
  lekin shunga yaqin bir nechta natija bor: …»
- **RU:** «Данных, точно соответствующих вашим условиям, не найдено, но есть
  несколько близких результатов: …»

Masalan «2018-yilda nechta bitim?» → 2018 yo'qligini aytib, yil filtrini olib
tashlab, barcha davrlar statistikasini **yaqin natija** sifatida ko'rsatadi.
`relax_sql()` oy → yil → hudud filtrini ketma-ket kengaytiradi. Haqiqatan yo'q
narsa (masalan Bitcoin) uchun esa aniq «ma'lumot mavjud emas» deyiladi.

---

## 🏗 Arxitektura

```
Telegram / Veb (UZ/RU savol)
   → bot.py / web/app.py
   → uzex/agent.py:
       1. detect_language (uz/ru, default uz)
       2. Gemini → JSON {sql, explanation, language}  (retry/backoff/fallback)
          ↓ kalit yo'q / API o'lik → uzex/heuristic.py (deterministik regex)
       3. search_entities (FTS5: participants+brokers+deals+contracts) → grounding
       4. is_safe_select (faqat SELECT/WITH, read-only)
       5. execute_sql → 0 qator bo'lsa relax_sql (near-match)
       6. format_answer: jadval + SQL + hisob-kitob + ⏱ ms
   → uzex.db (SQLite, 500k bitim, FTS5, deals_enriched view)
```

**Formulalar:** `Hajm = Soni × Lot`, `Birlik narxi = Summa ÷ Hajm`,
`Eksport USD→so'm = Summa × kurs` (`usd_rates`, bitim sanasi bo'yicha).

---

## 🚀 Ishga tushirish

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Baza: data/uzex.db.gz repoda bor. Ochish:
gunzip -kf data/uzex.db.gz          # -> data/uzex.db (~330 MB)
# yoki CSV dan qayta qurish: python -m uzex.etl --src uzex_spot_data --db data/uzex.db

# .env (namuna: .env.example)
#   UZEX_GEMINI_API_KEY=...   (bo'lmasa heuristic ishlaydi)
#   TELEGRAM_TOKEN=...        (Telegram bot uchun)
#   BOT_MODE=uzex

# Veb prototip (hakamlar uchun ochiq havola, ro'yxatdan o'tishsiz):
python -m uvicorn web.app:app --host 0.0.0.0 --port 8000

# Telegram bot (polling):
python bot.py

# Testlar:
python tests/test_uzex_fixes.py
```

### Deploy
- **Vercel:** `vercel.json` + `api/webhook.py` (webhook). `data/uzex.db.gz`
  bundle qilinadi, `/tmp` ga ochiladi. Env: `TELEGRAM_TOKEN`, `UZEX_GEMINI_API_KEY`,
  `BOT_MODE=uzex`, `WEBHOOK_SECRET`.
- **Render/Railway/VPS:** `python bot.py` (polling) yoki `uvicorn web.app:app`.
  Bot offline bo'lmasligi uchun `Restart=always` / health-check ishlating.

---

## ✅ Hakamlar tekshiruv ro'yxati (5 band)

1. **Havola ishlaydi** — veb `0.0.0.0`, boshqa qurilmadan ochiladi.
2. **Kirish ochiq** — ro'yxatdan o'tish/o'rnatish yo'q.
3. **Ikki til** — UZ/RU avtomatik aniqlanadi.
4. **Javoblar aniq** — SQL + read-only + 0 qatorda «ma'lumot yo'q»; yaqin
   natijalar aniq belgilanadi; gallyutsinatsiya yo'q.
5. **Forma yuborildi** — havolani Google Formaga kiritib, formani **yuboring**.

## 📁 Asosiy fayllar
```
uzex/agent.py      Text-to-SQL agent (Gemini + retry + near-match + guard)
uzex/heuristic.py  Deterministik fallback (kalitsiz / API o'lik)
uzex/db.py         Xavfsiz SQL, search_entities (barcha FTS), ensure_db
uzex/schema.py     LLM uchun sxema + few-shot
uzex/textnorm.py   ufold (Unicode LIKE), prepare_sql, search_hints
uzex/etl.py        CSV -> SQLite ETL
bot.py             Telegram bot (chunking, UZEX/USTA rejim)
api/webhook.py     Vercel webhook
web/app.py         FastAPI veb-chat
tests/             Regressiya testlari (audit + near-match)
```
