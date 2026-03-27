"""Webhook Leadbox CRM.

Recebe eventos do Leadbox (ticket fechado, mudança de fila, mensagens)
e controla pausa/despausa da IA via Redis.

IDs Aluga-Ar (tenant 123):
  - tenant_id: 123
  - queue_ia: 537 (Ana IA)
  - Atendimento: queue_id=453, user_id=815 (Nathália) ou 813 (Lázaro)
  - Financeiro: queue_id=454, user_id=814 (Tieli)
  - Cobranças: queue_id=544, user_id=814 (Tieli)
"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from infra.redis import get_redis_service, AGENT_ID
from infra.supabase import get_supabase

router = APIRouter()
logger = logging.getLogger(__name__)

# ── Config Leadbox (Aluga-Ar, tenant 123) ──
TENANT_ID = 123
QUEUE_IA = 537

# Filas onde a IA responde
IA_QUEUES = {QUEUE_IA}


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
        supabase.table("ana_leads").update({
            "ticket_id": None,
            "current_queue_id": QUEUE_IA,
            "current_user_id": None,
            "current_state": "ai",
            "paused_at": None,
            "paused_by": None,
            "responsavel": "AI",
        }).eq("telefone", phone).execute()
    except Exception as e:
        logger.error(f"[LEADBOX:{phone}] Erro ao resetar lead: {e}")

    # Limpar pausa no Redis
    await redis.pause_clear(phone)

    logger.info(f"[LEADBOX:{phone}] Ticket fechado → IA reativada")
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
        # Fila IA → despausar
        update_data["current_state"] = "ai"
        update_data["paused_at"] = None
        update_data["paused_by"] = None
        update_data["responsavel"] = "AI"
        await redis.pause_clear(phone)
        logger.info(f"[LEADBOX:{phone}] Fila IA ({queue_id}) → despausado")

    else:
        # Fila humana com atendente humano → PAUSAR
        update_data["current_state"] = "human"
        update_data["paused_at"] = now
        update_data["paused_by"] = f"leadbox_queue_{queue_id}"
        update_data["responsavel"] = "Humano"
        await redis.pause_set(phone)
        logger.info(f"[LEADBOX:{phone}] Fila humana ({queue_id}, user={user_id}) → PAUSADO")

    try:
        supabase.table("ana_leads").update(update_data) \
            .eq("telefone", phone).execute()
    except Exception as e:
        logger.error(f"[LEADBOX:{phone}] Erro ao atualizar lead: {e}")

    return {"status": "ok", "event": "queue_change"}


@router.post("/leadbox")
async def leadbox_webhook(request: Request):
    """Recebe eventos do Leadbox CRM."""
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "reason": "invalid_json"}

    event_type = body.get("event") or body.get("type") or "unknown"

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

    # Ticket fechado? (3 condições)
    if phone and (
        event_type == "FinishedTicket"
        or ticket_status == "closed"
        or (event_type == "UpdateOnTicket" and queue_id is None)
    ):
        return await handle_ticket_closed(phone, ticket_id)

    # Mudança de fila?
    if phone and queue_id:
        return await handle_queue_change(
            phone, int(queue_id), user_id, ticket_id
        )

    return {"status": "ok"}
