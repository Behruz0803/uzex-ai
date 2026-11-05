"""AI Usta bot — Vercel serverless WEBHOOK nuqtasi (24/7, doim ishlaydi).

Telegram yangilanishlarni POST /api/webhook ga yuboradi.
Har bir so'rov: update -> PTB process_update -> javob "ok".

Vercel Python runtime talabi: entrypoint `app` (WSGI) bo'lishi kerak
(qarang: pyproject.toml -> [tool.vercel] entrypoint = "api.webhook:app").
Shu bilan birga klassik `handler` (BaseHTTPRequestHandler) ham qoldirilgan.

Foydali GET so'rovlar (brauzerda ochib tekshirish uchun):
  GET /api/webhook            -> {"status":"ok", ...} health-check
  GET /api/webhook?setup=1    -> webhookni shu domenga (qayta) o'rnatadi
  GET /api/webhook?info=1     -> Telegram'dagi joriy webhook holati

Kerakli env (Vercel → Settings → Environment Variables):
  TELEGRAM_TOKEN  — bot tokeni (BotFather)
  GEMINI_API_KEY  — Google Gemini kaliti
  WEBHOOK_SECRET  — ixtiyoriy, webhook xavfsizlik tokeni
  WEBHOOK_URL     — ixtiyoriy; bo'lmasa VERCEL_PROJECT_PRODUCTION_URL /
                    VERCEL_URL dan olinadi (masalan https://ai-usta-bot-....vercel.app)
  LOG_CHAT_ID     — ixtiyoriy, log guruh ID'si
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

# Vercel'da funksiya fayli api/ ichida — loyiha ildizini (bot.py, ai_engine.py)
# import yo'liga qo'shamiz (lokal ishga tushirishda ham zarari yo'q).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from telegram import BotCommand, Update  # noqa: E402
from telegram.ext import ApplicationBuilder  # noqa: E402

import bot as bot_module  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uzex.webhook")


class WrongBotError(RuntimeError):
    """Token shu deploy kutilgan botga tegishli emas. Javob yuborilmaydi."""


def _token():
    return os.getenv("TELEGRAM_TOKEN", "")


def _secret():
    return os.getenv("WEBHOOK_SECRET", "")


# ── Yagona doimiy event-loop (alohida threadda) ─────────────────────
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="ptb-loop").start()


def _run(coro, timeout=55):
    """Kotunani doimiy loop'da sinxron bajarish."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


_app = None
_ready = False
_lock = threading.Lock()
_seen = deque(maxlen=1024)


async def _identity_ok(app):
    """Shu deploy faqat o'z botiga javob bersin. Boshqa token bo'lsa webhook qo'yilmaydi."""
    me = await app.bot.get_me()
    expected = (getattr(bot_module, "EXPECTED_BOT_USERNAME", "") or "").lstrip("@")
    if expected and (me.username or "") != expected:
        logger.error(
            "Webhook o'rnatilmadi: token @%s ga tegishli, kutilgan @%s",
            me.username, expected,
        )
        return False
    return True


async def _build_and_init():
    app = ApplicationBuilder().token(_token()).updater(None).build()
    bot_module.register_handlers(app)
    await app.initialize()
    await app.start()
    if not await _identity_ok(app):
        await app.stop()
        await app.shutdown()
        raise WrongBotError("telegram token boshqa botga tegishli, javob yuborilmaydi")
    await bot_module.post_init(app)          # /start /yangi /yordam buyruqlari
    return app


def _webhook_url():
    base = (
        os.getenv("WEBHOOK_URL")
        or os.getenv("VERCEL_PROJECT_PRODUCTION_URL")
        or os.getenv("VERCEL_URL", "")
    )
    if base and not base.startswith("http"):
        base = "https://" + base
    return f"{base.rstrip('/')}/api/webhook" if base else ""


def _ensure_ready(setup_webhook=True):
    global _app, _ready
    if _ready:
        return _app
    with _lock:
        if _ready:
            return _app
        _app = _run(_build_and_init())
        if setup_webhook:
            url = _webhook_url()
            if url and _token():
                _run(_app.bot.set_webhook(
                    url=url,
                    secret_token=_secret() or None,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                ))
                logger.info("Webhook o'rnatildi: %s", url)
        _ready = True
        return _app


# ── Umumiy mantiq: (status, content_type, body) qaytaradi ──────────
def _is_usta_path(path):
    p = (path or "").rstrip("/")
    return p == "/api/usta" or p.endswith("/usta")


def _usta_token():
    return os.getenv("USTA_TELEGRAM_TOKEN", "").strip()


def _usta_secret():
    return os.getenv("USTA_WEBHOOK_SECRET", "").strip()


def _usta_webhook_url():
    base = (
        os.getenv("USTA_WEBHOOK_URL")
        or os.getenv("VERCEL_PROJECT_PRODUCTION_URL")
        or "ai-usta-bot-omega.vercel.app"
    )
    if base and not base.startswith("http"):
        base = "https://" + base
    return f"{base.rstrip('/')}/api/usta"


_usta_app = None
_usta_ready = False
_usta_lock = threading.Lock()
_usta_seen = deque(maxlen=1024)


async def _build_usta():
    token = _usta_token()
    if not token or token.split(":", 1)[0] != str(bot_module.USTA_BOT_ID):
        raise WrongBotError("USTA_TELEGRAM_TOKEN AI Usta botiga tegishli emas")
    app = (
        ApplicationBuilder()
        .token(token)
        .updater(None)
        .connect_timeout(20)
        .read_timeout(90)
        .write_timeout(90)
        .pool_timeout(20)
        .media_write_timeout(120)
        .build()
    )
    bot_module.register_handlers(app)
    await app.initialize()
    await app.start()
    me = await app.bot.get_me()
    if me.id != bot_module.USTA_BOT_ID:
        await app.stop()
        await app.shutdown()
        raise WrongBotError(f"kutilmagan bot @{me.username} id={me.id}")
    await app.bot.set_my_commands([
        BotCommand("start", "Boshlash"),
        BotCommand("yangi", "Yangi muammo"),
        BotCommand("yordam", "Yordam"),
    ])
    try:
        await app.bot.set_my_short_description(
            short_description="Maishiy texnika va elektronika. Birja boti emas."
        )
    except Exception:
        logger.exception("usta tavsifi yangilanmadi")
    return app


def _ensure_usta():
    global _usta_app, _usta_ready
    if _usta_ready:
        return _usta_app
    with _usta_lock:
        if _usta_ready:
            return _usta_app
        _usta_app = _run(_build_usta())
        _usta_ready = True
        return _usta_app


_gemini_probe = {"done": False, "rows": None}


async def _probe_gemini():
    """Bir marta: qaysi model javob berishini tekshiradi. Kalit qaytmaydi."""
    if _gemini_probe["done"]:
        return _gemini_probe["rows"]
    from ai_engine import get_client, kvota_xatosimi, model_yoqmi, usta_model_nomlari
    rows = []
    try:
        client = get_client()
    except Exception as exc:
        rows = [{"model": "", "ok": False, "kind": type(exc).__name__}]
        _gemini_probe["done"] = True
        _gemini_probe["rows"] = rows
        return rows
    for model in usta_model_nomlari()[:5]:
        try:
            resp = await client.aio.models.generate_content(
                model=model,
                contents="ok",
                config={"max_output_tokens": 4},
            )
            rows.append({"model": model, "ok": True, "has_text": bool(getattr(resp, "text", None))})
            break
        except Exception as exc:
            if kvota_xatosimi(exc):
                kind = "quota"
            elif model_yoqmi(exc):
                kind = "missing"
            else:
                kind = type(exc).__name__
            rows.append({"model": model, "ok": False, "kind": kind})
    _gemini_probe["done"] = True
    _gemini_probe["rows"] = rows
    return rows


def _handle_usta(method, path, query, headers, raw_body):
    """Faqat @ai_usta_bot. UZEX tokeni va webhooki bu yerga kirmaydi."""
    if method == "GET":
        qs = parse_qs(query or "")
        try:
            if not _usta_token():
                return 500, "text/plain", "USTA_TELEGRAM_TOKEN not set"
            _ensure_usta()
            if "setup" in qs:
                url = _usta_webhook_url()
                _run(_usta_app.bot.set_webhook(
                    url=url,
                    secret_token=_usta_secret() or None,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                ))
                me = _run(_usta_app.bot.get_me())
                return 200, "application/json", json.dumps({
                    "status": "ok",
                    "bot": f"@{me.username}",
                    "mode": "usta",
                    "webhook_set_to": url,
                })
            if "info" in qs:
                info = _run(_usta_app.bot.get_webhook_info())
                me = _run(_usta_app.bot.get_me())
                log_chat = bot_module.usta_log_chat_id()
                log_status = "unset"
                if log_chat:
                    try:
                        chat = _run(_usta_app.bot.get_chat(log_chat))
                        log_status = f"ok:{getattr(chat, 'type', '')}"
                    except Exception as exc:
                        log_status = f"fail:{type(exc).__name__}"
                        logger.warning("usta log chat ochilmadi: %s", exc)
                return 200, "application/json", json.dumps({
                    "status": "ok",
                    "bot": f"@{me.username}",
                    "bot_id": me.id,
                    "mode": "usta",
                    "webhook_url": info.url,
                    "pending_updates": info.pending_update_count,
                    "last_error": info.last_error_message,
                    "expected_url": _usta_webhook_url(),
                    "log_forward": log_status,
                    "gemini": _run(_probe_gemini()) if "gemini" in qs else None,
                }, ensure_ascii=False)
        except WrongBotError as exc:
            logger.error("usta: noto'g'ri token, javob yuborilmaydi")
            prefix = _usta_token().split(":", 1)[0] if _usta_token() else ""
            return 200, "application/json", json.dumps({
                "status": "rejected",
                "reason": str(exc),
                "token_set": bool(_usta_token()),
                "token_prefix": prefix,
            })
        except Exception:
            logger.exception("usta GET xatosi")
            return 500, "text/plain", "error"
        return 200, "application/json", json.dumps({
            "status": "ok", "bot": "ai_usta_bot", "mode": "usta",
            "webhook_url": _usta_webhook_url(),
        })

    if method != "POST":
        return 405, "text/plain", "method not allowed"

    secret = _usta_secret()
    if secret:
        got = headers.get("x-telegram-bot-api-secret-token", "")
        if got != secret:
            return 403, "text/plain", "forbidden"
    if not _usta_token():
        return 500, "text/plain", "USTA_TELEGRAM_TOKEN not set"
    try:
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8")
        data = json.loads(raw_body or "{}")
    except Exception:
        return 400, "text/plain", "bad request"
    try:
        _ensure_usta()
    except WrongBotError:
        return 200, "text/plain", "ok"
    except Exception:
        logger.exception("usta init xatosi")
        return 500, "text/plain", "bot init error"

    update_id = data.get("update_id")
    if update_id in _usta_seen:
        return 200, "text/plain", "ok"
    _usta_seen.append(update_id)

    async def _run_update():
        bot_module.request_mode.set("usta")
        update = Update.de_json(data, _usta_app.bot)
        if update is not None:
            await _usta_app.process_update(update)

    try:
        _run(_run_update(), timeout=240)
    except Exception:
        logger.exception("usta update xatosi, update_id=%s", update_id)
    return 200, "text/plain", "ok"


def _handle(method, path, query, headers, raw_body):
    if _is_usta_path(path):
        return _handle_usta(method, path, query, headers, raw_body)
    if method == "GET":
        qs = parse_qs(query or "")
        try:
            if "setup" in qs:
                if not _token():
                    return 500, "text/plain", "TELEGRAM_TOKEN not set"
                _ensure_ready(setup_webhook=False)
                url = _webhook_url()
                _run(_app.bot.set_webhook(
                    url=url,
                    secret_token=_secret() or None,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                ))
                return 200, "application/json", json.dumps(
                    {"status": "ok", "webhook_set_to": url})
            if "db" in qs:
                from uzex.db import ensure_db, get_schema_summary
                import time
                t0 = time.time()
                path = ensure_db()
                return 200, "application/json", json.dumps({
                    "status": "ok",
                    "db": str(path),
                    "size_mb": round(path.stat().st_size / 1024 / 1024, 1),
                    "summary": get_schema_summary(),
                    "elapsed_s": round(time.time() - t0, 2),
                }, ensure_ascii=False)
            if "info" in qs:
                if not _token():
                    return 500, "text/plain", "TELEGRAM_TOKEN not set"
                _ensure_ready(setup_webhook=False)
                info = _run(_app.bot.get_webhook_info())
                me = _run(_app.bot.get_me())
                return 200, "application/json", json.dumps({
                    "status": "ok",
                    "bot": f"@{me.username}" if me else None,
                    "webhook_url": info.url,
                    "pending_updates": info.pending_update_count,
                    "last_error": info.last_error_message,
                    "expected_url": _webhook_url(),
                }, ensure_ascii=False)
        except Exception:
            logger.exception("GET %s xatosi", path)
            return 500, "text/plain", "error (loglarni tekshiring)"
        return 200, "application/json", json.dumps(
            {"status": "ok",
             "bot": getattr(bot_module, "EXPECTED_BOT_USERNAME", "uzex"),
             "mode": getattr(bot_module, "BOT_MODE", "uzex"),
             "webhook_url": _webhook_url()})

    if method != "POST":
        return 405, "text/plain", "method not allowed"

    # Telegram webhook secret tekshiruvi
    secret = _secret()
    if secret:
        got = headers.get("x-telegram-bot-api-secret-token", "")
        if got != secret:
            return 403, "text/plain", "forbidden"

    if not _token():
        return 500, "text/plain", "TELEGRAM_TOKEN not set"

    try:
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8")
        data = json.loads(raw_body or "{}")
    except Exception:
        logger.exception("update'ni o'qib bo'lmadi")
        return 400, "text/plain", "bad request"

    try:
        _ensure_ready()
    except WrongBotError:
        # 200 — Telegram qayta yubormasin va boshqa botdan javob ketmasin.
        logger.error("boshqa bot tokeni, update e'tiborsiz qoldirildi")
        return 200, "text/plain", "ok"
    except Exception:
        # 500 qaytaramiz — Telegram update'ni keyinroq qayta yuboradi
        logger.exception("botni ishga tushirib bo'lmadi (token/env tekshiring)")
        return 500, "text/plain", "bot init error"

    update_id = data.get("update_id")
    if update_id in _seen:                   # Telegram retry'dan himoya
        return 200, "text/plain", "ok"
    _seen.append(update_id)

    try:
        update = Update.de_json(data, _app.bot)
        if update is not None:
            _run(_app.process_update(update))
    except Exception:
        logger.exception("update processing error, update_id=%s", update_id)
    return 200, "text/plain", "ok"


# ── Vercel entrypoint: WSGI `app` (yangi Python runtime) ────────────
_STATUS_TEXT = {
    200: "200 OK", 400: "400 Bad Request", 403: "403 Forbidden",
    405: "405 Method Not Allowed", 500: "500 Internal Server Error",
}


def app(environ, start_response):
    """WSGI application — Vercel [tool.vercel] entrypoint shu funksiyani yuklaydi."""
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    # Bitta entrypoint ikkala manzilni ham olishi mumkin. /usta ni adashtirmaslik.
    for key, value in environ.items():
        if isinstance(value, str) and "/usta" in value:
            path = "/api/usta"
            break
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


# ── Klassik Vercel handler (BaseHTTPRequestHandler) ────────────────
class handler(BaseHTTPRequestHandler):
    """Eski/klassik Vercel Python runtime bilan ham ishlashi uchun."""

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

    def log_message(self, format, *args):  # shovqinni kamaytirish
        logger.info("%s - %s", self.address_string(), format % args)
