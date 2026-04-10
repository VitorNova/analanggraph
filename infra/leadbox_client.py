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

# Cache de credenciais Meta (preenchido em _get_meta_credentials)
_meta_token = None
_meta_phone_id = None


def _get_sync_redis() -> sync_redis.Redis:
    """Retorna pool Redis sync singleton."""
    global _sync_pool
    if _sync_pool is None:
        _sync_pool = sync_redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
    return _sync_pool


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
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                LEADBOX_EXTERNAL_URL,
                params={"token": LEADBOX_API_TOKEN},
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            logger.info("[LEADBOX] Resposta enviada para %s", phone)
            # Marker para ignorar eco fromMe (webhook volta com a msg da IA)
            _mark_sent_by_ia(phone)
            return True
    except Exception as e:
        logger.error("[LEADBOX] Erro ao enviar resposta para %s: %s", phone, e, exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "envio_falhou", str(e)[:300], {"payload_size": len(mensagem)})
        return False



def _get_meta_credentials() -> tuple[str, str]:
    """Busca token Meta e phone_id do Leadbox (cacheado em módulo)."""
    global _meta_token, _meta_phone_id
    if _meta_token and _meta_phone_id:
        return _meta_token, _meta_phone_id
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{LEADBOX_API_URL}/whatsapp/430",
                headers={"Authorization": f"Bearer {LEADBOX_API_TOKEN}"},
            )
            resp.raise_for_status()
            data = resp.json()
            _meta_token = data["tokenAPI"]
            _meta_phone_id = data["phoneId"]
            return _meta_token, _meta_phone_id
    except Exception as e:
        logger.error(f"[META] Falha ao buscar credenciais Meta: {e}")
        return "", ""


def enviar_template_leadbox(
    phone: str,
    template_id: str,
    params: list,
    body_texto: str = "",
    queue_id: int = None,
    user_id: int = None,
) -> bool:
    """Envia template oficial do WhatsApp via Meta Cloud API + registra no Leadbox.

    Dois passos:
    1. Meta Cloud API envia o template (entrega garantida fora da janela 24h)
    2. Leadbox POST PUSH registra o texto no CRM e move o ticket para a fila

    Args:
        phone: Telefone do destinatário (com DDI, ex: '5566999990000').
        template_id: Nome do template aprovado no WhatsApp (ex: 'cobranca').
        params: Lista de parâmetros na ordem do template ({{1}}, {{2}}, ...).
        body_texto: Texto legível para registrar no Leadbox (aparece na conversa).
        queue_id: Fila destino no Leadbox (ex: 544 para billing).
        user_id: Usuário destino no Leadbox (ex: 1095 para IA billing).
    """
    # --- Passo 1: Enviar template via Meta Cloud API ---
    meta_token, phone_id = _get_meta_credentials()
    if not meta_token:
        logger.error("[META] Sem credenciais Meta, não pode enviar template")
        return False

    parameters = [{"type": "text", "text": p} for p in params]

    meta_payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_id,
            "language": {"code": "pt_BR"},
            "components": [
                {"type": "body", "parameters": parameters}
            ],
        },
    }

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"https://graph.facebook.com/v19.0/{phone_id}/messages",
                headers={
                    "Authorization": f"Bearer {meta_token}",
                    "Content-Type": "application/json",
                },
                json=meta_payload,
            )
            resp.raise_for_status()
            data = resp.json()
            msg_id = (data.get("messages") or [{}])[0].get("id", "?")
            logger.info(f"[META] Template '{template_id}' enviado para {phone} (wamid={msg_id})")
            _mark_sent_by_ia(phone)
    except Exception as e:
        logger.error(f"[META] Erro ao enviar template '{template_id}' para {phone}: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "envio_falhou", str(e)[:300], {"template_id": template_id})
        return False

    # --- Passo 2: Registrar no Leadbox (CRM tracking + mover fila) ---
    if body_texto or queue_id:
        leadbox_payload = {
            "number": phone,
            "externalKey": phone,
            "body": body_texto,
        }
        if queue_id:
            leadbox_payload["queueId"] = queue_id
            leadbox_payload["forceTicketToDepartment"] = True
        if user_id:
            leadbox_payload["userId"] = user_id
            leadbox_payload["forceTicketToUser"] = True

        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    LEADBOX_EXTERNAL_URL,
                    params={"token": LEADBOX_API_TOKEN},
                    headers={"Content-Type": "application/json"},
                    json=leadbox_payload,
                )
                resp.raise_for_status()
                logger.info(f"[LEADBOX] Registrado no CRM: {phone} → fila {queue_id}")
                _mark_sent_by_ia(phone)
        except Exception as e:
            # Template já foi enviado via Meta — só falhou o registro no Leadbox
            logger.warning(f"[LEADBOX] Falha ao registrar no CRM para {phone}: {e}")

    return True


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
