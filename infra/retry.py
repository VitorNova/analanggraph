"""
Retry com backoff exponencial para invocação do grafo LangGraph.

Tenta invocar o grafo até MAX_TENTATIVAS vezes, com delays crescentes
entre tentativas. Retorna o resultado ou None se todas falharam.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

MAX_TENTATIVAS = 3
BACKOFF_DELAYS = [2.0, 4.0, 8.0]


async def invocar_com_retry(graph, payload: dict, phone: str = "", max_tentativas: int = None, backoff_delays: list = None) -> tuple:
    """
    Invoca graph.ainvoke com retry e backoff exponencial.

    Args:
        graph: Grafo LangGraph compilado
        payload: Dict com messages e phone para graph.ainvoke
        phone: Telefone do lead (para logging)
        max_tentativas: Número máximo de tentativas (default: MAX_TENTATIVAS)
        backoff_delays: Lista de delays em segundos (default: BACKOFF_DELAYS)

    Returns:
        Tupla (result, last_error) onde result é o dict do graph ou None
    """
    if max_tentativas is None:
        max_tentativas = MAX_TENTATIVAS
    if backoff_delays is None:
        backoff_delays = BACKOFF_DELAYS

    result = None
    last_error = None

    for tentativa in range(max_tentativas):
        try:
            result = await graph.ainvoke(payload)
            break
        except Exception as e:
            last_error = e
            logger.error(
                f"[GRAFO:{phone}] Erro tentativa {tentativa+1}/{max_tentativas}: {e}",
                exc_info=True,
            )
            if tentativa < max_tentativas - 1:
                delay = backoff_delays[tentativa] if tentativa < len(backoff_delays) else backoff_delays[-1]
                logger.info(f"[GRAFO:{phone}] Retry em {delay}s...")
                await asyncio.sleep(delay)

    if result is None and last_error is not None:
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "retry_esgotado", f"{max_tentativas} tentativas: {last_error}"[:300])

    return result, last_error
