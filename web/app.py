# -*- coding: utf-8 -*-
"""
FastAPI Web UI for UZEX AI Agent
Provides browser chat interface + REST API
Can be deployed to Vercel/Render/Railway or run locally
"""
import asyncio
import pathlib
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Try import uzex agent
try:
    from uzex.agent import answer as uzex_answer
    from uzex.db import get_db_path
    UZEX_ENABLED = True
except Exception as e:
    uzex_answer = None
    UZEX_ENABLED = False
    print(f"UZEX not enabled in web: {e}")

app = FastAPI(title="UZEX AI Agent", description="Text-to-SQL over 500k deals", version="1.0")

# Static and templates
web_dir = pathlib.Path(__file__).parent
# Ensure templates exists
templates = None
try:
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=str(web_dir / "templates"))
except:
    pass

class QueryRequest(BaseModel):
    question: str
    user_id: int | None = None

class QueryResponse(BaseModel):
    question: str
    answer: str
    elapsed_ms: int | None = None

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Inline HTML if template not found
    template_file = web_dir / "templates" / "index.html"
    if template_file.exists() and templates:
        return templates.TemplateResponse("index.html", {"request": request})
    # Fallback inline
    html = """
<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UZEX AI Agent — 500k bitimlar</title>
<style>
*{box-sizing:border-box} body{font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0b1026;color:#e8f0ff;margin:0;padding:0}
.header{background:linear-gradient(135deg,#0b1026,#1a2a6a);padding:32px 20px;text-align:center;border-bottom:1px solid #233}
.header h1{margin:0;font-size:28px;color:#fff}
.header p{color:#9bb;margin:8px 0 0}
.container{max-width:900px;margin:0 auto;padding:20px}
.card{background:#131a3a;border:1px solid #223; border-radius:12px; padding:16px; margin:16px 0}
.chat{height:420px; overflow-y:auto; background:#0d1330; border:1px solid #223; border-radius:12px; padding:12px}
.msg{margin:10px 0; padding:12px 14px; border-radius:10px; max-width:85%}
.user{background:#1a5cff; color:#fff; margin-left:auto; text-align:right}
.bot{background:#1e2a5a; color:#e8f0ff}
.input-row{display:flex; gap:8px; margin-top:12px}
.input-row input{flex:1; padding:12px; border-radius:10px; border:1px solid #334; background:#0d1330; color:#fff; font-size:15px}
.input-row button{padding:12px 16px; border-radius:10px; border:0; background:#1a5cff; color:#fff; font-weight:600; cursor:pointer}
.examples{display:flex; flex-wrap:wrap; gap:8px}
.examples button{background:#16204a; color:#9bb; border:1px solid #334; padding:8px 12px; border-radius:20px; cursor:pointer; font-size:13px}
.examples button:hover{background:#1a2a6a;color:#fff}
pre{background:#0a0f2a; padding:12px; border-radius:8px; overflow-x:auto; font-size:13px}
.sql{color:#5fd06e}
.badge{display:inline-block; background:#1a5cff; color:#fff; padding:2px 8px; border-radius:20px; font-size:12px; margin-right:6px}
.footer{text-align:center; color:#6a7; font-size:12px; margin:24px}
a{color:#5fd06e}
</style>
</head>
<body>
<div class="header">
  <h1>📈 UZEX AI Agent</h1>
  <p>500 000 ta bitim (2022-2026) bo'yicha Text-to-SQL — har javobda SQL va hisob-kitob bilan</p>
  <p style="font-size:13px;color:#8af">O'zbek va Rus tillarini tushunadi • Anti-hallucination • 90 sekunddan kam (odatda 2-3 sek)</p>
</div>
<div class="container">
  <div class="card">
    <h3 style="margin:0 0 8px">💡 Savol namunalari (bosish bilan yuboriladi)</h3>
    <div class="examples">
      <button onclick="ask('2024 yilda eng koʻp bitim qilgan broker kim?')">🏆 Top broker 2024</button>
      <button onclick="ask('BK001 brokeri 2025 yilda nechta bitimda ishtirok etgan?')">BK001 2025</button>
      <button onclick="ask('Экспортные сделки в 2024 году сумма в сумах?')">💵 Eksport 2024</button>
      <button onclick="ask('Sement boʻyicha eng qimmat bitim qaysi 2023 yilda?')">🏗 Sement 2023</button>
      <button onclick="ask('STIR 921912462 boʻyicha jami savdo qancha?')">STIR misol</button>
      <button onclick="ask('Paxta yogʻi boʻyicha 2025 yilda eng arzon shartnoma qaysi?')">Paxta yog'i</button>
      <button onclick="ask('Qaysi tovar eng koʻp sotilgan 2024 yilda?')">Top tovar</button>
    </div>
  </div>

  <div class="card">
    <div id="chat" class="chat">
      <div class="msg bot">👋 Salom! Men UZEX birja ma'lumotlari bo'yicha AI yordamchiman. Savol bering, masalan:<br>• <i>2024 yilda eng ko'p bitim qilgan broker kim?</i><br>• <i>Экспортные сделки в 2024 сумма?</i><br><br>Har javobda SQL va hisob-kitobni ko'rsataman — anti-hallucination!</div>
    </div>
    <div class="input-row">
      <input id="q" placeholder="Savol yozing... (uz yoki ru)" onkeydown="if(event.key==='Enter') send()">
      <button onclick="send()">Yuborish ➤</button>
    </div>
  </div>

  <div class="card">
    <h4 style="margin:0 0 8px">📊 Texnik ma'lumot</h4>
    <p style="font-size:13px;color:#9bb">Baza: <span class="badge">500k bitim</span> <span class="badge">6.6k shartnoma</span> <span class="badge">14k kompaniya</span> <span class="badge">120 broker</span></p>
    <p style="font-size:13px">Arxitektura: <b>Text-to-SQL + SQLite + FTS5 + Gemini</b> • Har savol → deterministik SELECT • 0 qator bo'lsa → <code>Ma'lumotlar bazasida bunday ma'lumot mavjud emas</code> (diskvalifikatsiyadan himoya)</p>
    <p style="font-size:13px">Formulalar: <code>Hajm = Soni × Lot</code> • <code>Birlik narxi = Summa ÷ (Soni × Lot)</code> • <code>Eksport USD→so'm = Summa × kurs</code></p>
    <p style="font-size:12px;color:#6a8">Telegram bot: <a href="https://t.me/uzexaihackaton_bot" target="_blank">@uzexaihackaton_bot</a></p>
  </div>
  <div class="footer">UZEX AI Hackathon — 5 noyabr 18:00 gacha • Prototip sifatiga 20 ball: SQL + manba ko'rsatilgan</div>
</div>
<script>
const chat=document.getElementById('chat');
const input=document.getElementById('q');
function addMsg(text, cls){
  const d=document.createElement('div');
  d.className='msg '+cls;
  d.innerHTML=text.replace(/\\n/g,'<br>');
  chat.appendChild(d);
  chat.scrollTop=chat.scrollHeight;
}
async function send(){
  const q=input.value.trim();
  if(!q) return;
  addMsg(q,'user');
  input.value='';
  addMsg('⏳ Qidirilmoqda... (SQL tuzilmoqda)','bot');
  try{
    const res=await fetch('/api/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});
    const data=await res.json();
    chat.lastChild.remove();
    // Render markdown-like
    let html=data.answer
      .replace(/```sql([\\s\\S]*?)```/g,'<pre class=\"sql\">$1</pre>')
      .replace(/\\| (.+) \\|/g,'<code>$1</code>');
    addMsg(html.replace(/\\n/g,'<br>'),'bot');
  }catch(e){
    chat.lastChild.remove();
    addMsg('⚠️ Xato: '+e.message,'bot');
  }
}
function ask(t){ input.value=t; send(); }
</script>
</div>
</body>
</html>
    """
    return HTMLResponse(content=html)

@app.post("/api/query")
async def query_api(req: QueryRequest):
    if not UZEX_ENABLED or not uzex_answer:
        return JSONResponse({"answer": "⚠️ UZEX baza yuklanmagan. `python -m uzex.etl --src /tmp/uzex_spot_data --db ./data/uzex.db` ni ishga tushiring.", "sql": "", "rows": 0}, status_code=500)
    import time
    start = time.time()
    ans = await uzex_answer(req.question, req.user_id)
    elapsed = int((time.time()-start)*1000)
    return {"question": req.question, "answer": ans, "elapsed_ms": elapsed}

@app.get("/api/health")
async def health():
    try:
        dbp = get_db_path() if UZEX_ENABLED else None
        exists = dbp.exists() if dbp else False
        size = dbp.stat().st_size if exists else 0
        return {"status": "ok", "uzex_enabled": UZEX_ENABLED, "db_exists": exists, "db_size_mb": round(size/1024/1024,1) if exists else 0, "db_path": str(dbp) if dbp else None}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.get("/api/stats")
async def stats():
    if not UZEX_ENABLED:
        return {"error": "UZEX not enabled"}
    import sqlite3
    dbp = get_db_path()
    if not dbp.exists():
        return {"error": f"DB not found at {dbp}"}
    conn = sqlite3.connect(str(dbp))
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM deals;")
    deals = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM contracts;")
    contracts = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM participants;")
    parts = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM brokers;")
    brokers = cur.fetchone()[0]
    cur.execute("SELECT min(deal_date), max(deal_date) FROM deals;")
    min_d, max_d = cur.fetchone()
    conn.close()
    return {"deals": deals, "contracts": contracts, "participants": parts, "brokers": brokers, "period": f"{min_d} → {max_d}"}

# For Vercel: expose app

