"""
Template: Redis Service para agente IA.

Baseado em: /var/www/agente-langgraph/infra/redis.py (produção)

Funcionalidades:
- Buffer de mensagens (RPUSH/LRANGE/DEL)
- Lock distribuído (SET NX EX)
- Controle de pausa
- Context de mídia

Uso:
    redis = await get_redis_service()
    await redis.buffer_add_message(phone, {"texto": "oi"})
    msgs = await redis.buffer_get_messages(phone)
"""

import json
import logging
import os
from typing import List, Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)

BUFFER_DELAY_SECONDS = 9
DEFAULT_TTL_SECONDS = 300
LOCK_TTL_SECONDS = 60
AGENT_ID = os.environ.get("AGENT_ID", "ana-langgraph")


class RedisService:

    def __init__(self, redis_url: str = None):
        self._redis_url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379")
        self._client: Optional[redis.Redis] = None

    async def connect(self):
        if self._client is None:
            self._client = redis.from_url(self._redis_url, encoding="utf-8", decode_responses=True)
            await self._client.ping()
            logger.info("[REDIS] Conectado")

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            raise RuntimeError("Redis não conectado. Chame connect() primeiro.")
        return self._client

    # ── Keys ──

    def _buffer_key(self, phone: str) -> str:
        return f"buffer:msg:{AGENT_ID}:{phone}"

    def _lock_key(self, phone: str) -> str:
        return f"lock:msg:{AGENT_ID}:{phone}"

    def _pause_key(self, phone: str) -> str:
        return f"pause:{AGENT_ID}:{phone}"

    def _context_key(self, phone: str) -> str:
        return f"context:{AGENT_ID}:{phone}"

    # ── Buffer ──

    async def buffer_add_message(self, phone: str, message_data: dict, ttl: int = DEFAULT_TTL_SECONDS):
        key = self._buffer_key(phone)
        await self.client.rpush(key, json.dumps(message_data, ensure_ascii=False))
        await self.client.expire(key, ttl)

    async def buffer_get_messages(self, phone: str) -> List[dict]:
        key = self._buffer_key(phone)
        raw = await self.client.lrange(key, 0, -1)
        return [json.loads(m) for m in raw]

    async def buffer_clear(self, phone: str):
        await self.client.delete(self._buffer_key(phone))

    # ── Lock ──

    async def lock_acquire(self, phone: str, ttl: int = LOCK_TTL_SECONDS) -> bool:
        return await self.client.set(self._lock_key(phone), "1", nx=True, ex=ttl)

    async def lock_release(self, phone: str) -> bool:
        return await self.client.delete(self._lock_key(phone)) > 0

    async def lock_exists(self, phone: str) -> bool:
        return await self.client.exists(self._lock_key(phone)) > 0

    # ── Pausa ──

    async def pause_set(self, phone: str, ttl: int = 86400):
        key = self._pause_key(phone)
        await self.client.set(key, "1", ex=ttl)
        logger.info(f"[REDIS] Pausado: {phone} (TTL={ttl}s)")

    async def pause_clear(self, phone: str) -> bool:
        deleted = await self.client.delete(self._pause_key(phone))
        logger.info(f"[REDIS] Despausa: {phone} (existia: {deleted > 0})")
        return deleted > 0

    async def is_paused(self, phone: str) -> bool:
        return await self.client.exists(self._pause_key(phone)) > 0

    # ── Snooze (billing) ──

    def _snooze_key(self, phone: str, context_type: str = "billing") -> str:
        return f"snooze:{context_type}:{AGENT_ID}:{phone}"

    async def snooze_set(self, phone: str, until_date: str, context_type: str = "billing"):
        """Seta snooze: silencia disparos até until_date (ISO YYYY-MM-DD).

        TTL calculado automaticamente: (until_date - hoje + 1 dia de margem).
        """
        from datetime import date, timedelta
        key = self._snooze_key(phone, context_type)
        target = date.fromisoformat(until_date)
        today = date.today()
        days_until = (target - today).days + 1  # +1 dia de margem
        ttl = max(days_until * 86400, 86400)  # mínimo 24h
        await self.client.set(key, until_date, ex=ttl)
        logger.info(f"[REDIS] Snooze {context_type}:{phone} até {until_date} (TTL {days_until}d)")

    async def snooze_get(self, phone: str, context_type: str = "billing") -> str:
        """Retorna data do snooze ou None."""
        return await self.client.get(self._snooze_key(phone, context_type))

    async def is_snoozed(self, phone: str, context_type: str = "billing") -> bool:
        """Verifica se phone está em snooze ATIVO (data >= hoje)."""
        from datetime import date
        val = await self.client.get(self._snooze_key(phone, context_type))
        if not val:
            return False
        try:
            return date.fromisoformat(val) >= date.today()
        except ValueError:
            return False

    # ── Context ──

    async def save_context(self, phone: str, context: dict, ttl: int = DEFAULT_TTL_SECONDS):
        await self.client.set(self._context_key(phone), json.dumps(context), ex=ttl)

    async def get_context(self, phone: str) -> Optional[dict]:
        raw = await self.client.get(self._context_key(phone))
        return json.loads(raw) if raw else None


# ── Singleton ──

_service: Optional[RedisService] = None


async def get_redis_service() -> RedisService:
    global _service
    if _service is None:
        _service = RedisService()
        await _service.connect()
    return _service
