"""Webhookni qo'lda o'rnatish / tekshirish / o'chirish (lokal kompyuterdan).

Ishlatish (avval .env faylga TELEGRAM_TOKEN va WEBHOOK_URL yozing):

    python scripts/set_webhook.py            # webhook holatini ko'rish
    python scripts/set_webhook.py set        # webhookni WEBHOOK_URL/api/webhook ga o'rnatish
    python scripts/set_webhook.py delete     # webhookni o'chirish (polling rejimiga qaytish)

Eslatma: webhook o'rnatilgan bo'lsa, bot.py polling rejimida ishlamaydi —
Telegram yangilanishlarni faqat webhook manziliga yuboradi. Shuning uchun
24/7 rejimda bot.py ni lokalda ishga tushirmang.
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
BASE = os.getenv("WEBHOOK_URL", "").rstrip("/")
SECRET = os.getenv("WEBHOOK_SECRET", "")

if not TOKEN:
    sys.exit("❌ TELEGRAM_TOKEN .env faylda topilmadi")

api = f"https://api.telegram.org/bot{TOKEN}"
cmd = sys.argv[1] if len(sys.argv) > 1 else "info"

if cmd == "set":
    if not BASE:
        sys.exit("❌ WEBHOOK_URL .env faylda topilmadi (masalan https://ai-usta-bot-....vercel.app)")
    payload = {"url": f"{BASE}/api/webhook", "allowed_updates": ["message", "callback_query"]}
    if SECRET:
        payload["secret_token"] = SECRET
    r = requests.post(f"{api}/setWebhook", json=payload, timeout=30)
    print(r.json())
elif cmd == "delete":
    print(requests.post(f"{api}/deleteWebhook", timeout=30).json())
else:
    print(requests.get(f"{api}/getWebhookInfo", timeout=30).json())
    me = requests.get(f"{api}/getMe", timeout=30).json()
    print("Bot:", me.get("result", {}).get("username"))
