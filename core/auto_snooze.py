"""
Auto-snooze billing.

Quando o contexto é COBRANCA e a Ana não transferiu nem registrou compromisso,
aplica snooze de 48h para silenciar disparos automáticos de billing.
"""

import logging
from datetime import date, timedelta

from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)


async def auto_snooze_billing(phone: str, context_type: str, novas_mensagens: list, redis_service) -> None:
    """
    Aplica auto-snooze billing 48h se contexto é billing e Ana não agiu.

    Args:
        phone: Telefone do lead
        context_type: Tipo de contexto ("billing", "manutencao", ou None)
        novas_mensagens: Mensagens novas do resultado do graph
        redis_service: Instância do RedisService
    """
    if context_type != "billing":
        return

    has_transfer = any(
        isinstance(m, AIMessage) and m.tool_calls
        and any(tc["name"] == "transferir_departamento" for tc in m.tool_calls)
        for m in novas_mensagens
    )
    has_snooze_tool = any(
        isinstance(m, AIMessage) and m.tool_calls
        and any(tc["name"] == "registrar_compromisso" for tc in m.tool_calls)
        for m in novas_mensagens
    )

    if has_transfer or has_snooze_tool:
        return

    try:
        snooze_date = (date.today() + timedelta(days=2)).isoformat()
        await redis_service.snooze_set(phone, snooze_date, "billing")
        logger.info(f"[GRAFO:{phone}] Auto-snooze billing 48h até {snooze_date}")
        from infra.event_logger import log_event
        log_event("auto_snooze", phone, until=snooze_date)
    except Exception as e:
        logger.error(f"[GRAFO:{phone}] Auto-snooze falhou: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "snooze_falhou", str(e)[:300])
