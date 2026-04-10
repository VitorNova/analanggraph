"""Webhook Leadbox CRM.

Recebe eventos do Leadbox (ticket fechado, mudança de fila, mensagens)
e controla pausa/despausa da IA via Redis.

Evento NewMessage de cliente → buffer → grafo → resposta via API Leadbox.

IDs Aluga-Ar (tenant 123):
  - tenant_id: 123
  - queue_ia: 537 (Ana IA)
  - Atendimento: queue_id=453, user_id=815 (Nathália) ou 813 (Lázaro)
  - Financeiro: queue_id=454, user_id=814 (Tieli)
  - Cobranças: queue_id=544, user_id=814 (Tieli)
"""

import asyncio
import base64
import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request

from core.constants import (
    TABLE_LEADS, TENANT_ID, IA_QUEUES, QUEUE_IA,
)
from infra.redis import get_redis_service
from infra.supabase import get_supabase
from infra.event_logger import log_event
router = APIRouter()
logger = logging.getLogger(__name__)


_buffer_initialized = False


async def _init_buffer():
    """Inicializa buffer com callback de processamento (lazy)."""
    global _buffer_initialized
    if _buffer_initialized:
        return

    from core.grafo import processar_mensagens
    from infra.buffer import get_message_buffer

    buffer = await get_message_buffer()
    buffer.set_process_callback(processar_mensagens)
    _buffer_initialized = True
    logger.info("[LEADBOX] Buffer inicializado")


def _baixar_midia_base64(media_url: str, timeout: int = 15):
    """Baixa mídia de uma URL e retorna como base64."""
    if not media_url:
        return None
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(media_url)
            resp.raise_for_status()
            return base64.b64encode(resp.content).decode("utf-8")
    except Exception as e:
        logger.error(f"[LEADBOX] Erro ao baixar mídia: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente("desconhecido", "media_erro", str(e)[:300], {"url": media_url[:100]})
        return None


async def handle_new_message(phone: str, texto: str, nome: str, ticket_id,
                             media_type: str = None, media_url: str = None,
                             media_mimetype: str = None, media_name: str = None):
    """Mensagem do cliente via Leadbox → buffer → grafo."""
    if not texto and not media_url:
        return {"status": "ignored", "reason": "empty_text_no_media"}

    from infra.buffer import get_message_buffer
    from infra.nodes_supabase import upsert_lead

    await _init_buffer()

    # Upsert lead
    upsert_lead(phone, nome)

    # Montar msg no formato padrão
    msg_data = {
        "telefone": phone,
        "texto": texto,
        "nome": nome,
        "canal": "leadbox",
    }

    # Baixar mídia se presente
    if media_url and media_type:
        media_b64 = _baixar_midia_base64(media_url)
        if media_b64:
            if media_type == "audio":
                msg_data["audio_base64"] = media_b64
                msg_data["audio_mimetype"] = media_mimetype or "audio/ogg"
                logger.info(f"[LEADBOX:{phone}] Áudio baixado ({len(media_b64)//1024}KB b64)")
            elif media_type == "image":
                msg_data["imagem_base64"] = media_b64
                msg_data["imagem_mimetype"] = media_mimetype or "image/jpeg"
                logger.info(f"[LEADBOX:{phone}] Imagem baixada ({len(media_b64)//1024}KB b64)")
            elif media_type in ("document", "application"):
                msg_data["documento_base64"] = media_b64
                msg_data["documento_mimetype"] = media_mimetype or "application/pdf"
                msg_data["documento_nome"] = media_name or ""
                logger.info(f"[LEADBOX:{phone}] Documento baixado ({len(media_b64)//1024}KB b64)")
            else:
                logger.warning(f"[LEADBOX:{phone}] Tipo de mídia não suportado: {media_type}")
        else:
            logger.warning(f"[LEADBOX:{phone}] Falha ao baixar mídia {media_type}")

    buffer = await get_message_buffer()
    await buffer.add_message(phone, msg_data, context={"nome": nome})

    desc = texto[:80] if texto else f"[{media_type}]"
    logger.info(f"[LEADBOX:{phone}] NewMessage bufferizada: {desc}")
    return {"status": "buffered", "event": "new_message"}


async def handle_ticket_closed(phone: str, ticket_id):
    """Ticket fechou → reset lead para estado IA."""
    redis = await get_redis_service()
    supabase = get_supabase()
    if not supabase:
        return {"status": "error", "reason": "supabase_unavailable"}

    # Race condition: aguardar se IA está processando
    if await redis.lock_exists(phone):
        for _ in range(6):
            await asyncio.sleep(0.5)
            if not await redis.lock_exists(phone):
                break

    # Reset lead no Supabase
    try:
        supabase.table(TABLE_LEADS).update({
            "ticket_id": None,
            "current_queue_id": QUEUE_IA,
            "current_user_id": None,
            "current_state": "ai",
            "paused_at": None,
            "paused_by": None,
            "responsavel": "AI",
        }).eq("telefone", phone).execute()
    except Exception as e:
        logger.error(f"[LEADBOX:{phone}] Erro ao resetar lead: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "lead_reset_erro", str(e)[:300])

    # Limpar pausa no Redis
    await redis.pause_clear(phone)

    logger.info(f"[LEADBOX:{phone}] Ticket fechado → IA reativada")
    log_event("ticket_closed", phone)
    return {"status": "ok", "event": "ticket_closed"}


async def handle_queue_change(phone: str, queue_id: int, user_id, ticket_id):
    """Lead mudou de fila → pausar ou despausar IA."""
    redis = await get_redis_service()
    supabase = get_supabase()
    if not supabase:
        return {"status": "error", "reason": "supabase_unavailable"}

    now = datetime.now(timezone.utc).isoformat()

    update_data = {
        "current_queue_id": queue_id,
        "current_user_id": user_id,
        "ticket_id": ticket_id,
        "updated_at": now,
    }

    if queue_id in IA_QUEUES:
        # Fila IA → despausar, MAS respeitar pausa por humano (fromMe)
        # UpdateOnTicket na mesma fila IA NÃO deve anular pausa de humano
        try:
            lead_row = supabase.table(TABLE_LEADS).select(
                "paused_by"
            ).eq("telefone", phone).limit(1).execute()
            paused_by = (lead_row.data[0].get("paused_by") or "") if lead_row.data else ""
        except Exception as e:
            logger.warning(f"[LEADBOX:{phone}] Falha ao checar paused_by: {e}")
            paused_by = ""

        if paused_by == "human_fromMe":
            logger.info(f"[LEADBOX:{phone}] Fila IA ({queue_id}) mas paused_by=human_fromMe → mantém pausado")
            # Atualizar apenas metadata (queue/ticket), não mexer na pausa
            update_data["current_queue_id"] = queue_id
            update_data["current_user_id"] = user_id
            update_data["ticket_id"] = ticket_id
        else:
            update_data["current_state"] = "ai"
            update_data["paused_at"] = None
            update_data["paused_by"] = None
            update_data["responsavel"] = "AI"
            await redis.pause_clear(phone)
            logger.info(f"[LEADBOX:{phone}] Fila IA ({queue_id}) → despausado")
            log_event("unpaused", phone, queue_id=queue_id)

    else:
        # Fila humana com atendente humano → PAUSAR
        update_data["current_state"] = "human"
        update_data["paused_at"] = now
        update_data["paused_by"] = f"leadbox_queue_{queue_id}"
        update_data["responsavel"] = "Humano"
        await redis.pause_set(phone)
        logger.info(f"[LEADBOX:{phone}] Fila humana ({queue_id}, user={user_id}) → PAUSADO")
        log_event("paused", phone, queue_id=queue_id, user_id=user_id)

    try:
        supabase.table(TABLE_LEADS).update(update_data) \
            .eq("telefone", phone).execute()
    except Exception as e:
        logger.error(f"[LEADBOX:{phone}] Erro ao atualizar lead: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "pausa_erro", str(e)[:300], {"queue_id": queue_id})

    return {"status": "ok", "event": "queue_change"}


@router.post("/leadbox")
async def leadbox_webhook(request: Request):
    """Recebe eventos do Leadbox CRM."""
    try:
        body = await request.json()
    except Exception as e:
        logger.warning(f"[LEADBOX] JSON inválido no webhook: {e}")
        from infra.incidentes import registrar_incidente
        registrar_incidente("desconhecido", "webhook_erro", f"JSON inválido: {e}")
        return {"status": "error", "reason": "invalid_json"}

    event_type = body.get("event") or body.get("type") or "unknown"

    # ⛔ CAPTURA: salvar TODOS os webhooks raw — antes de qualquer filtro
    try:
        import json as _json
        from pathlib import Path
        debug_file = Path("/var/www/ana-langgraph/logs/webhook_payloads.jsonl")
        debug_file.parent.mkdir(parents=True, exist_ok=True)
        with open(debug_file, "a", encoding="utf-8") as f:
            f.write(_json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "raw": body}, default=str, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # Filtrar eventos irrelevantes
    if event_type in {"AckMessage", "FinishedTicketHistoricMessages"}:
        return {"status": "ignored"}

    # Extrair dados (multi-level fallback)
    message = body.get("message") or body.get("data", {}).get("message") or {}
    ticket = message.get("ticket") or body.get("ticket") or {}
    contact = ticket.get("contact") or message.get("contact") or {}

    queue_id = ticket.get("queueId") or message.get("queueId")
    user_id = ticket.get("userId") or message.get("userId")
    ticket_id = ticket.get("id") or message.get("ticketId")
    phone = contact.get("number", "").replace("+", "").strip()
    phone = "".join(filter(str.isdigit, phone))
    ticket_status = ticket.get("status", "")
    tenant_id_payload = body.get("tenantId") or ticket.get("tenantId")

    logger.info(
        f"[LEADBOX] event={event_type} phone={phone} queue={queue_id} "
        f"user={user_id} ticket={ticket_id} tenant={tenant_id_payload}"
    )

    # Filtrar por tenant
    if tenant_id_payload and int(tenant_id_payload) != TENANT_ID:
        return {"status": "ignored", "reason": "wrong_tenant"}

    # Ticket fechado? (2 condições confiáveis)
    if phone and (
        event_type == "FinishedTicket"
        or ticket_status == "closed"
    ):
        return await handle_ticket_closed(phone, ticket_id)

    # NewMessage do cliente → buffer → grafo
    if event_type == "NewMessage" and phone:
        from_me = message.get("fromMe", False)
        texto = (message.get("body") or "").strip()
        nome = contact.get("name") or contact.get("pushName") or ""

        if from_me:
            send_type = message.get("sendType")

            # Camada 1: marker Redis (TTL 15s) — setado por todos os 7 pontos de envio da IA
            redis = await get_redis_service()
            agent_id = os.environ.get("AGENT_ID", "ana-langgraph")
            marker_key = f"sent:ia:{agent_id}:{phone}"
            is_ia_echo = await redis.client.exists(marker_key)

            if is_ia_echo:
                await redis.client.delete(marker_key)
                logger.info(f"[LEADBOX:{phone}] fromMe eco IA (marker) — ignorando")
                return {"status": "ok", "event": "ia_echo"}

            # Camada 2: sendType=API é sempre a IA (fallback se marker expirou)
            if send_type == "API":
                logger.info(f"[LEADBOX:{phone}] fromMe sendType=API (IA) — ignorando")
                return {"status": "ok", "event": "ia_echo_api"}

            # Camada 3: sem marker + sendType != API → humano real → PAUSAR IA
            logger.info(f"[LEADBOX:{phone}] fromMe HUMANO (sendType={send_type}, userId={message.get('userId')}) → pausando IA")
            supabase = get_supabase()
            if not await redis.is_paused(phone):
                await redis.pause_set(phone)
                now = datetime.now(timezone.utc).isoformat()
                if supabase:
                    try:
                        supabase.table(TABLE_LEADS).update({
                            "current_state": "human",
                            "paused_at": now,
                            "paused_by": "human_fromMe",
                            "responsavel": "Humano",
                            "updated_at": now,
                        }).eq("telefone", phone).execute()
                    except Exception as e:
                        logger.error(f"[LEADBOX:{phone}] Erro ao pausar por fromMe: {e}", exc_info=True)
                logger.info(f"[LEADBOX:{phone}] NewMessage fromMe → IA PAUSADA (humano assumiu)")
                log_event("paused", phone, reason="human_fromMe")
            else:
                logger.info(f"[LEADBOX:{phone}] NewMessage fromMe — já pausado")
            return {"status": "ok", "event": "human_takeover"}

        # Extrair mídia
        media_type = message.get("mediaType")  # "audio", "image", "document"
        media_url = message.get("mediaUrl")
        media_name = message.get("mediaName") or message.get("originalName") or ""
        # Mimetype: raw.audio.mime_type / raw.image.mime_type / etc
        raw = message.get("raw") or {}
        if not isinstance(raw, dict):
            raw = {}
        raw_media = (raw.get(media_type) or {}) if media_type else {}
        if not isinstance(raw_media, dict):
            raw_media = {}
        media_mimetype = raw_media.get("mime_type")

        desc = texto[:80] if texto else f"[{media_type}]" if media_type else "[vazio]"
        logger.info(f"[LEADBOX:{phone}] NewMessage: {desc}")

        return await handle_new_message(
            phone, texto, nome, ticket_id,
            media_type=media_type, media_url=media_url,
            media_mimetype=media_mimetype, media_name=media_name,
        )

    # Mudança de fila?
    if phone and queue_id:
        return await handle_queue_change(
            phone, int(queue_id), user_id, ticket_id
        )

    return {"status": "ok"}
