"""
Template: Detector de contexto no conversation_history.

Baseado em: apps/ia/app/domain/messaging/context/context_detector.py (produção)

Varre últimas 10 mensagens de trás pra frente buscando campo "context".
Usado antes de invocar o grafo para saber se lead está em contexto de billing/manutenção.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Mapeamento de contextos (normaliza nomes variantes)
CONTEXT_MAPPING = {
    "billing": "billing",
    "disparo_billing": "billing",
    "disparo_cobranca": "billing",
    "manutencao_preventiva": "manutencao",
    "disparo_manutencao": "manutencao",
    "manutencao": "manutencao",
}


def detect_context(
    conversation_history: dict,
    max_age_hours: int = 168,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Detecta contexto ativo no histórico de conversa.

    Varre as últimas 10 mensagens de trás pra frente.
    Retorna o primeiro contexto encontrado que não expirou.

    Args:
        conversation_history: JSONB {"messages": [...]}
        max_age_hours: Janela máxima em horas (default 168 = 7 dias)

    Returns:
        (context_type, reference_id) ou (None, None)
    """
    messages = (conversation_history or {}).get("messages", [])
    if not messages:
        logger.debug("[CONTEXT] Histórico vazio")
        return None, None

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)

    # Varrer últimas 10 de trás pra frente (mais recente primeiro)
    for msg in reversed(messages[-10:]):
        raw_context = msg.get("context")
        if not raw_context:
            continue

        # Verificar expiração
        timestamp_str = msg.get("timestamp")
        if timestamp_str:
            try:
                ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                if ts < cutoff:
                    logger.debug(f"[CONTEXT] Contexto '{raw_context}' expirado ({timestamp_str})")
                    continue
            except (ValueError, TypeError):
                pass  # Timestamp inválido, aceitar

        # Normalizar contexto
        context_type = CONTEXT_MAPPING.get(raw_context, raw_context)
        reference_id = (
            msg.get("reference_id")
            or msg.get("contract_id")
            or msg.get("payment_id")
        )

        logger.info(f"[CONTEXT] Detectado: {context_type} (ref={reference_id})")
        return context_type, reference_id

    logger.debug("[CONTEXT] Nenhum contexto encontrado")
    return None, None


def build_context_prompt(context_type: str, reference_id: str = None) -> str:
    """
    Gera prompt extra baseado no contexto detectado.

    Args:
        context_type: "billing" ou "manutencao"
        reference_id: ID da cobrança/contrato

    Returns:
        Prompt extra para injetar no system_prompt
    """
    if context_type == "billing":
        return f"""## CONTEXTO ATIVO: COBRANÇA
O cliente recebeu disparo automático de cobrança (ref: {reference_id or 'N/A'}).
Ele está respondendo sobre PAGAMENTO.

REGRAS PARA ESTE CONTEXTO:
- NÃO peça CPF — use a ferramenta de consulta com buscar_por_telefone=true
- Se disser que já pagou ("já paguei", "fiz o pix", "mandei comprovante") → transfira para Financeiro/Tieli (fila 454, atendente 814) IMEDIATAMENTE. NÃO consulte, NÃO verifique, NÃO peça CPF.
- O link de pagamento já foi enviado na mensagem anterior do histórico — se pedir Pix/link, reenvie o link REAL que aparece lá
- Se quiser negociar → transfira para financeiro
- Se tiver dúvida sobre valor → use a ferramenta de consulta com buscar_por_telefone=true
- Se prometer pagar em uma data ("vou pagar sexta", "pago amanhã", "essa semana", "vou pagar depois") → use a ferramenta de registro de compromisso com a data em YYYY-MM-DD. NÃO diga "anotado" ou "registrei" sem usar a ferramenta PRIMEIRO. Converta a fala do lead para data ISO real. Se vago ("essa semana", "depois"), use a próxima sexta-feira.
"""

    if context_type == "manutencao":
        return f"""## CONTEXTO ATIVO: MANUTENÇÃO PREVENTIVA
O cliente recebeu aviso de manutenção preventiva (contrato: {reference_id or 'N/A'}).
Ele está respondendo sobre AGENDAMENTO DE MANUTENÇÃO.

REGRAS PARA ESTE CONTEXTO:
- NÃO peça CPF — o lead já está identificado
- Se o cliente mencionar DEFEITO (ar fazendo barulho, pingando, não gelando, parou, quebrado, não liga, não esfria, vazando) → transfira para Atendimento/Nathália (fila 453, atendente 815) IMEDIATAMENTE. NÃO peça CPF, NÃO consulte. Apenas transfira. Defeito NÃO é manutenção preventiva.
- Se NÃO for defeito → pergunte dia e horário de preferência para a visita técnica
- Se quiser reagendar → pergunte novo dia/horário
- Se RECUSAR a manutenção ("não preciso", "não quero", "tá tudo ok", "não") → transfira para Atendimento/Nathália (fila 453, atendente 815) IMEDIATAMENTE, sem insistir. A empresa precisa registrar a recusa.
- Manutenção preventiva é GRATUITA (inclusa no contrato)
"""

    return ""
