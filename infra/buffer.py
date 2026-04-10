"""
Template: Message Buffer com agendamento.

Baseado em: /var/www/agente-langgraph/infra/buffer.py (produção)

Agrupa mensagens do mesmo lead antes de processar.
Delay de 9s — reinicia se nova msg chega.

Uso:
    buffer = await get_message_buffer()
    buffer.set_process_callback(processar_mensagens)
    await buffer.add_message(phone, {"texto": "oi"})
"""

import asyncio
import logging
from typing import Dict, Optional, Callable, Awaitable

from infra.redis import RedisService, get_redis_service, BUFFER_DELAY_SECONDS

logger = logging.getLogger(__name__)


class MessageBuffer:

    buffer_delay: int = BUFFER_DELAY_SECONDS
    _scheduled_tasks: Dict[str, asyncio.Task] = {}
    _processing_keys: set = set()

    def __init__(self, redis_service: Optional[RedisService] = None):
        self._redis = redis_service
        self._process_callback: Optional[Callable] = None

    async def _get_redis(self) -> RedisService:
        if self._redis is None:
            self._redis = await get_redis_service()
        return self._redis

    def set_process_callback(self, callback: Callable[..., Awaitable]):
        """Define callback chamado após buffer expirar."""
        self._process_callback = callback

    async def add_message(self, phone: str, message_data: dict, context: dict = None):
        """Adiciona msg ao buffer e agenda processamento."""
        redis = await self._get_redis()

        # Verificar pausa
        if await redis.is_paused(phone):
            logger.info(f"[BUFFER:{phone}] IA pausada - ignorando")
            return

        # Adicionar ao Redis
        await redis.buffer_add_message(phone, message_data)

        # Salvar contexto se fornecido
        if context:
            await redis.save_context(phone, context)

        # Agendar processamento
        await self._schedule_processing(phone)

        logger.info(f"[BUFFER:{phone}] Mensagem adicionada (delay={self.buffer_delay}s)")

    async def _schedule_processing(self, phone: str):
        """Agenda processamento com delay. Cancela anterior se existir."""
        # Se está processando, não interferir
        if phone in self._processing_keys:
            logger.debug(f"[BUFFER:{phone}] Em processamento, msg será pega no próximo ciclo")
            return

        # Cancelar task anterior se ainda está no sleep
        existing = self._scheduled_tasks.get(phone)
        if existing and not existing.done():
            existing.cancel()

        # Nova task com delay
        task = asyncio.create_task(self._delayed_process(phone))
        self._scheduled_tasks[phone] = task

    async def _delayed_process(self, phone: str):
        """Aguarda delay e processa."""
        try:
            await asyncio.sleep(self.buffer_delay)
            await self._process_buffered_messages(phone)
        except asyncio.CancelledError:
            logger.debug(f"[BUFFER:{phone}] Timer cancelado (nova msg chegou)")
        except Exception as e:
            logger.error(f"[BUFFER:{phone}] Erro: {e}", exc_info=True)

    async def _process_buffered_messages(self, phone: str):
        """Processa mensagens acumuladas."""
        if not self._process_callback:
            logger.warning("[BUFFER] Callback não definido!")
            return

        redis = await self._get_redis()

        # Adquirir lock
        if not await redis.lock_acquire(phone):
            logger.warning(f"[BUFFER:{phone}] Lock ocupado, pulando")
            return

        self._processing_keys.add(phone)

        try:
            # Ler mensagens (sem limpar — preserva se falhar)
            messages = await redis.buffer_get_messages(phone)
            if not messages:
                return

            # Safety cap: se buffer acumulou demais (falhas repetidas), limpar
            if len(messages) > 20:
                logger.warning(f"[BUFFER:{phone}] Buffer overflow ({len(messages)} msgs) - limpando")
                await redis.buffer_clear(phone)
                messages = messages[-5:]  # Processar só as últimas 5

            # Buscar contexto
            context = await redis.get_context(phone) or {}

            logger.info(f"[BUFFER:{phone}] Processando {len(messages)} msg(s)")

            # Chamar callback (processar_mensagens do grafo)
            await self._process_callback(phone, messages, context)

            # Sucesso: agora sim limpar o buffer
            await redis.buffer_clear(phone)

        except Exception as e:
            # Falha: buffer PRESERVADO para próximo processamento
            logger.error(f"[BUFFER:{phone}] Erro no callback, buffer PRESERVADO: {e}", exc_info=True)
            from infra.incidentes import registrar_incidente
            registrar_incidente(phone, "buffer_erro", str(e)[:300])

        finally:
            self._processing_keys.discard(phone)
            await redis.lock_release(phone)


# ── Singleton ──

_buffer: Optional[MessageBuffer] = None


async def get_message_buffer() -> MessageBuffer:
    global _buffer
    if _buffer is None:
        _buffer = MessageBuffer()
    return _buffer
