"""Persist private Telegram chat IDs in Supabase, without exposing them publicly."""
import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def save_user(chat_id: int) -> bool:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        logger.error("User storage is not configured")
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"{url}/rest/v1/ai_usta_bot_users?on_conflict=chat_id",
                headers={"apikey": key, "Authorization": f"Bearer {key}",
                         "Prefer": "resolution=ignore-duplicates,return=minimal"},
                json={"chat_id": chat_id},
            )
            response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("User storage failed: %s", type(exc).__name__)
        return False
