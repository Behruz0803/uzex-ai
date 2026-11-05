"""Send the one-time reminder to the explicitly provided private chat IDs."""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import urllib.parse

MESSAGE = (
    "Assalomu alaykum! Uskuna va texnikalaringiz joyidami? "
    "Hech qanday nosozlik yo‘qmi? Agar muammo bo‘lsa, bemalol menga "
    "xabar bering — birgalikda to‘g‘irlaymiz."
)


def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not token or not base or not key:
        print("Telegram and Supabase credentials are required", file=sys.stderr)
        return 1
    ids = []
    offset = 0
    while True:
        query = urllib.parse.urlencode({"select": "chat_id", "first_seen_at": "lt.2026-11-02T04:50:00Z", "order": "chat_id.asc", "limit": 1000, "offset": offset})
        req = urllib.request.Request(f"{base}/rest/v1/ai_usta_bot_users?{query}", headers={"apikey": key, "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                batch = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f"Could not load recipients: {type(exc).__name__}", file=sys.stderr)
            return 1
        ids.extend(str(row["chat_id"]) for row in batch)
        if len(batch) < 1000:
            break
        offset += len(batch)

    failed = 0
    for chat_id in ids:
        data = json.dumps({"chat_id": chat_id, "text": MESSAGE}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.load(response)
                if not result.get("ok"):
                    raise ValueError("Telegram rejected the message")
            print(f"Sent to chat {chat_id}")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # Never print the URL: it includes the bot token. Continue for other users.
            print(f"Failed to send to chat {chat_id}: {type(exc).__name__}", file=sys.stderr)
            failed += 1
        time.sleep(0.1)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
