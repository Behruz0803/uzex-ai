"""AI Usta — alohida webhook. UZEX botiga ulanmaydi.

Manzil: https://ai-usta-bot-omega.vercel.app/api/usta
Token:  USTA_TELEGRAM_TOKEN  (TELEGRAM_TOKEN emas)
Secret: USTA_WEBHOOK_SECRET (WEBHOOK_SECRET emas)

GET /api/usta          health
GET /api/usta?setup=1  shu botning webhookini shu manzilga qo'yadi
GET /api/usta?info=1   qaysi bot ulanganini ko'rsatadi
"""
import asyncio
import json
import logging
import os
import sys
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Vercel loyiha env'ida BOT_MODE=uzex turadi. Bu funksiya o'z jarayonida
# uni almashtiradi. UZEX funksiyasi boshqa jarayon, unga tegmaydi.
_USTA_TOKEN = os.environ.get("USTA_TELEGRAM_TOKEN", "").strip()
os.environ["BOT_MODE"] = "usta"
os.environ["EXPECTED_BOT_USERNAME"] = "ai_usta_bot"
os.environ["TELEGRAM_TOKEN"] = _USTA_TOKEN
# LOG_CHAT_ID o'chirilmaydi: mijoz xabarlari shu guruhga ketadi.
# Alohida guruh kerak bo'lsa USTA_LOG_CHAT_ID qo'yiladi.

if "bot" in sys.modules:
    raise RuntimeError("usta webhook UZEX jarayoni ichida yuklanmasin")

from telegram import Update  # noqa: E402
from telegram.ext import ApplicationBuilder  # noqa: E402

import bot as bot_module  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("usta.webhook")

# Bot id tokenning ochiq qismi. UZEX boti (9021053891) bu yerga kira olmaydi.
EXPECTED_BOT_ID = 8731876262
EXPECTED_USERNAME = "ai_usta_bot"


class WrongBotError(RuntimeError):
    pass


def _token():
    return os.getenv("USTA_TELEGRAM_TOKEN", "").strip()


def _secret():
    return os.getenv("USTA_WEBHOOK_SECRET", "").strip()


_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="usta-loop").start()


def _run(coro, timeout=55):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


_app = None
_ready = False
_lock = threading.Lock()
_seen = deque(maxlen=1024)


def _webhook_url():
    base = (
        os.getenv("USTA_WEBHOOK_URL")
        or os.getenv("VERCEL_PROJECT_PRODUCTION_URL")
        or "ai-usta-bot-omega.vercel.app"
    )
    if base and not base.startswith("http"):
        base = "https://" + base
    return f"{base.rstrip('/')}/api/usta"


async def _build_and_init():
    token = _token()
    if not token:
        raise RuntimeError("USTA_TELEGRAM_TOKEN yo'q")
    if token.split(":", 1)[0] != str(EXPECTED_BOT_ID):
        raise WrongBotError("bu token AI Usta botiga tegishli emas")
    app = ApplicationBuilder().token(token).updater(None).build()
    bot_module.register_handlers(app)
    await app.initialize()
    await app.start()
    me = await app.bot.get_me()
    if me.id != EXPECTED_BOT_ID or (me.username or "") != EXPECTED_USERNAME:
        await app.stop()
        await app.shutdown()
        raise WrongBotError(f"kutilmagan bot @{me.username}")
    if bot_module.BOT_MODE != "usta" or not bot_module.IS_USTA:
        await app.stop()
        await app.shutdown()
        raise WrongBotError("usta rejimi yoqilmagan")
    await bot_module.post_init(app)
    return app


def _ensure_ready(setup_webhook=False):
    global _app, _ready
    if _ready:
        return _app
    with _lock:
        if _ready:
            return _app
        _app = _run(_build_and_init())
        if setup_webhook:
            _run(_app.bot.set_webhook(
                url=_webhook_url(),
                secret_token=_secret() or None,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            ))
        _ready = True
        return _app


def _handle(method, path, query, headers, raw_body):
    if "webhook" in (path or "") and "usta" not in (path or ""):
        return 404, "text/plain", "not found"

    if method == "GET":
        qs = parse_qs(query or "")
        try:
            if "setup" in qs:
                if not _token():
                    return 500, "text/plain", "USTA_TELEGRAM_TOKEN not set"
                _ensure_ready(setup_webhook=False)
                url = _webhook_url()
                _run(_app.bot.set_webhook(
                    url=url,
                    secret_token=_secret() or None,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                ))
                me = _run(_app.bot.get_me())
                return 200, "application/json", json.dumps({
                    "status": "ok",
                    "bot": f"@{me.username}",
                    "webhook_set_to": url,
                })
            if "info" in qs:
                if not _token():
                    return 500, "text/plain", "USTA_TELEGRAM_TOKEN not set"
                _ensure_ready(setup_webhook=False)
                info = _run(_app.bot.get_webhook_info())
                me = _run(_app.bot.get_me())
                log_chat = bot_module.usta_log_chat_id()
                log_status = "unset"
                if log_chat:
                    try:
                        chat = _run(_app.bot.get_chat(log_chat))
                        log_status = f"ok:{getattr(chat, 'type', '')}"
                    except Exception as exc:
                        log_status = f"fail:{type(exc).__name__}"
                        logger.warning("usta log chat ochilmadi: %s", exc)
                return 200, "application/json", json.dumps({
                    "status": "ok",
                    "bot": f"@{me.username}" if me else None,
                    "bot_id": me.id if me else None,
                    "mode": "usta",
                    "webhook_url": info.url,
                    "pending_updates": info.pending_update_count,
                    "last_error": info.last_error_message,
                    "expected_url": _webhook_url(),
                    "log_forward": log_status,
                }, ensure_ascii=False)
        except WrongBotError:
            logger.error("usta webhook: noto'g'ri bot, javob yuborilmaydi")
            return 200, "text/plain", "ok"
        except Exception:
            logger.exception("usta GET xatosi")
            return 500, "text/plain", "error"
        return 200, "application/json", json.dumps({
            "status": "ok",
            "bot": EXPECTED_USERNAME,
            "mode": "usta",
            "webhook_url": _webhook_url(),
        })

    if method != "POST":
        return 405, "text/plain", "method not allowed"

    secret = _secret()
    if secret:
        got = headers.get("x-telegram-bot-api-secret-token", "")
        if got != secret:
            return 403, "text/plain", "forbidden"

    if not _token():
        return 500, "text/plain", "USTA_TELEGRAM_TOKEN not set"

    try:
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8")
        data = json.loads(raw_body or "{}")
    except Exception:
        return 400, "text/plain", "bad request"

    try:
        _ensure_ready()
    except WrongBotError:
        logger.error("usta: boshqa bot tokeni, update tashlab yuborildi")
        return 200, "text/plain", "ok"
    except Exception:
        logger.exception("usta init xatosi")
        return 500, "text/plain", "bot init error"

    update_id = data.get("update_id")
    if update_id in _seen:
        return 200, "text/plain", "ok"
    _seen.append(update_id)

    try:
        update = Update.de_json(data, _app.bot)
        if update is not None:
            _run(_app.process_update(update))
    except Exception:
        logger.exception("usta update xatosi, update_id=%s", update_id)
    return 200, "text/plain", "ok"


_STATUS_TEXT = {
    200: "200 OK", 400: "400 Bad Request", 403: "403 Forbidden",
    404: "404 Not Found", 405: "405 Method Not Allowed",
    500: "500 Internal Server Error",
}


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    query = environ.get("QUERY_STRING", "")
    headers = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            headers[key[5:].replace("_", "-").lower()] = value
    if environ.get("CONTENT_TYPE"):
        headers["content-type"] = environ["CONTENT_TYPE"]
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    raw_body = environ["wsgi.input"].read(length) if length > 0 else b""
    status, content_type, body = _handle(method, path, query, headers, raw_body)
    payload = body.encode("utf-8") if isinstance(body, str) else body
    start_response(_STATUS_TEXT.get(status, f"{status} Unknown"), [
        ("Content-Type", content_type),
        ("Content-Length", str(len(payload))),
    ])
    return [payload]


class handler(BaseHTTPRequestHandler):
    def _dispatch(self, method):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw_body = self.rfile.read(length) if length > 0 else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        status, content_type, body = _handle(
            method, parsed.path, parsed.query, headers, raw_body)
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)
