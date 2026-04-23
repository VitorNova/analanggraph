"""Client Leadbox — envio de mensagens via API externa.

Extraído de api/webhooks/leadbox.py para eliminar acoplamento circular.
"""

import logging
import os

import httpx
import redis as sync_redis

from core.constants import LEADBOX_API_URL, LEADBOX_API_UUID, LEADBOX_API_TOKEN

logger = logging.getLogger(__name__)

# URL de envio (API externa Leadbox)
LEADBOX_EXTERNAL_URL = f"{LEADBOX_API_URL}/v1/api/external/{LEADBOX_API_UUID}/"

AGENT_NAME = "Ana"

# Pool Redis sync compartilhado (singleton)
_sync_pool = None

# Mapeamento: nome do template → hsmId (ID na Meta)
# Usado por enviar_template_leadbox para enviar via Leadbox com template oficial
TEMPLATE_HSM_IDS = {
    "diavencimento": "1307792311201097",
    "cobranca": "1933898480565060",
    "diadovencimento": "1630599891513327",
    "15diasdeatraso": "936264969272307",
    "venceu1": "1619879289089264",
    "manutencao": "947986774486046",
    "inicial": "909981968711180",
    "pagamentoaprovado": "949538204672996",
    "reengajamento": "2703896383317249",
    "tielifinanceiro": "2982590331937548",
    "osalugaar": "1650967356237373",
}


def _get_sync_redis() -> sync_redis.Redis:
    """Retorna pool Redis sync singleton."""
    global _sync_pool
    if _sync_pool is None:
        _sync_pool = sync_redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
    return _sync_pool


# Pool HTTP sync compartilhado (singleton)
_http_client: httpx.Client = None


def _get_http_client() -> httpx.Client:
    """Retorna client HTTP sync singleton (connection pooling)."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(timeout=15)
    return _http_client


def enviar_resposta_leadbox(phone: str, mensagem: str, raw: bool = False,
                            queue_id: int = None, user_id: int = None) -> bool:
    """Envia resposta da IA ao cliente via API externa do Leadbox.

    Args:
        phone: Telefone do destinatário.
        mensagem: Texto da mensagem.
        raw: Se True, envia a mensagem exatamente como está (sem prefixo *Ana:*).
             Usar para templates de billing/manutenção que precisam bater exatamente
             com o template aprovado do WhatsApp.
        queue_id: Fila destino no Leadbox (atribui ticket à fila).
        user_id: Usuário destino no Leadbox (atribui ticket ao usuário).
    """
    if not LEADBOX_API_TOKEN:
        logger.warning("[LEADBOX] LEADBOX_API_TOKEN não configurado, pulando envio")
        return False

    # Assinatura do agente (só para respostas conversacionais, não templates)
    body = mensagem if raw else f"*{AGENT_NAME}:*\n{mensagem}"

    payload = {
        "body": body,
        "number": phone,
        "externalKey": phone,
    }
    if queue_id is not None:
        payload["queueId"] = queue_id
        payload["forceTicketToDepartment"] = True
    if user_id is not None:
        payload["userId"] = user_id
        payload["forceTicketToUser"] = True

    try:
        client = _get_http_client()
        resp = client.post(
            LEADBOX_EXTERNAL_URL,
            params={"token": LEADBOX_API_TOKEN},
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        logger.info("[LEADBOX] Resposta enviada para %s", phone)
        _mark_sent_by_ia(phone)
        return True
    except Exception as e:
        logger.error("[LEADBOX] Erro ao enviar resposta para %s: %s", phone, e, exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "envio_falhou", str(e)[:300], {"payload_size": len(mensagem)})
        return False



def enviar_template_leadbox(
    phone: str,
    template_id: str,
    params: list,
    body_texto: str = "",
    queue_id: int = None,
    user_id: int = None,
) -> bool:
    """Envia template oficial do WhatsApp via Leadbox (1 POST só).

    O Leadbox recebe o hsmId + params, chama a Meta Cloud API internamente,
    entrega no WhatsApp e registra no CRM automaticamente.

    Args:
        phone: Telefone do destinatário (com DDI, ex: '5566999990000').
        template_id: Nome do template aprovado no WhatsApp (ex: 'cobranca').
        params: Lista de parâmetros na ordem do template ({{1}}, {{2}}, ...).
        body_texto: (ignorado — Leadbox monta o body a partir do template).
        queue_id: Fila destino no Leadbox (ex: 544 para billing).
        user_id: Usuário destino no Leadbox (ex: 1095 para IA billing).
    """
    if not LEADBOX_API_TOKEN:
        logger.warning("[LEADBOX] LEADBOX_API_TOKEN não configurado, pulando envio")
        return False

    hsm_id = TEMPLATE_HSM_IDS.get(template_id)
    if not hsm_id:
        logger.error(f"[LEADBOX] Template '{template_id}' não encontrado em TEMPLATE_HSM_IDS")
        return False

    payload = {
        "number": phone,
        "externalKey": phone,
        "body": "",
        "templateId": hsm_id,
        "typeTemplate": "template",
        "params": [str(p) for p in params],
    }
    if queue_id is not None:
        payload["queueId"] = queue_id
        payload["forceTicketToDepartment"] = True
    if user_id is not None:
        payload["userId"] = user_id
        payload["forceTicketToUser"] = True

    try:
        client = _get_http_client()
        resp = client.post(
            LEADBOX_EXTERNAL_URL,
            params={"token": LEADBOX_API_TOKEN},
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        logger.info(f"[LEADBOX] Template '{template_id}' (hsm={hsm_id}) enviado para {phone}")
        _mark_sent_by_ia(phone)
        return True
    except Exception as e:
        logger.error(f"[LEADBOX] Erro ao enviar template '{template_id}' para {phone}: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "envio_falhou", str(e)[:300], {"template_id": template_id, "hsm_id": hsm_id})
        return False


def _mark_sent_by_ia(phone: str):
    """Grava marker no Redis para diferenciar eco da IA de mensagem humana."""
    try:
        r = _get_sync_redis()
        agent_id = os.environ.get("AGENT_ID", "ana-langgraph")
        r.set(f"sent:ia:{agent_id}:{phone}", "1", ex=15)
    except Exception as e:
        logger.warning(f"[LEADBOX] Falha ao marcar sent:ia para {phone}: {e}")
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "marker_ia_falhou", str(e)[:300])
