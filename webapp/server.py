"""
UZEX AI — public web prototype (FastAPI).

Judges' checklist:
  * Public link, no registration / installation.
  * Works in Uzbek and Russian.
  * Answers are grounded in the SQLite registry (SQL shown under each answer).

Run:
  python -m webapp.server          # http://0.0.0.0:8000
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import UzexAgent, detect_language

BASE = Path(__file__).parent
app = FastAPI(title="UZEX AI", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

agent = UzexAgent()
_sessions: dict[str, str] = {}

EXAMPLES = {
    "uz": [
        "2024-yilda birjada jami qancha so'mlik bitim tuzilgan?",
        "Eng ko'p bitim o'tkazgan TOP 3 broker qaysilar?",
        "Barcha davrlar bo'yicha eng katta aylanmaga ega TOP 3 sotuvchi kompaniya kim?",
        "Navoiy viloyatida 2024-yilda nechta bitim tuzilgan?",
        "BK001 brokeri orqali jami nechta bitim o'tkazilgan?",
        "2024-yilda sement bo'yicha nechta bitim tuzilgan?",
        "2018-yilda birjada nechta bitim tuzilgan?",
        "Eng qimmat tovar qaysi?",
    ],
    "ru": [
        "Какой общий объём сделок за 2024 год?",
        "Какие ТОП-3 брокера провели больше всего сделок?",
        "Крупнейший покупатель за всё время?",
        "Сколько сделок по цементу в 2024 году?",
        "Какой общий оборот по брокеру BK001?",
        "Сколько сделок было в 2018 году?",
        "В каком регионе больше всего сделок?",
        "Какой самый дорогой товар?",
    ],
}


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    lang: str | None = None
    session_id: str = "default"


@app.get("/api/health")
def health():
    meta = agent.meta
    return {
        "status": "ok",
        "database": "uzex.db",
        "coverage": {"from": meta.get("min_date"), "to": meta.get("max_date"),
                     "deals": int(meta.get("deal_count", 0))},
        "gemini_configured": agent.llm.available,
        "languages": ["uz", "ru"],
    }


@app.get("/api/examples")
def examples(lang: str = "uz"):
    return {"examples": EXAMPLES.get(lang, EXAMPLES["uz"])}


@app.post("/api/ask")
def ask(req: AskRequest):
    t0 = time.monotonic()
    default_lang = _sessions.get(req.session_id, "uz")
    lang = req.lang or detect_language(req.question, default=default_lang)
    if lang not in ("uz", "ru"):
        lang = "uz"
    _sessions[req.session_id] = lang
    answer = agent.answer(req.question, lang=lang, default_lang=default_lang)
    return JSONResponse({
        "answer": answer.text,
        "sql": answer.sql,
        "near_sql": answer.near_sql,
        "near_match": answer.near_match,
        "lang": answer.lang,
        "engine": answer.engine,
        "off_topic": answer.off_topic,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    })


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")))
