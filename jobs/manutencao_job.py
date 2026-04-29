"""Job de Manutenção Preventiva — Lembrete D-7.

Busca contratos com proxima_manutencao = hoje + 7 dias
e envia lembrete via WhatsApp com dados do equipamento.

Salva contexto "manutencao_preventiva" no histórico para que
o context_detector saiba que o lead está respondendo sobre manutenção.

Uso:
    python jobs/manutencao_job.py           # Roda manualmente
    PM2 cron: seg-sex às 9h (ecosystem.config.js)
"""

import asyncio
import sys
import logging
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from infra.supabase import get_supabase
from core.constants import TABLE_LEADS, TABLE_ASAAS_CLIENTES, TABLE_CONTRACT_DETAILS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Template oficial WhatsApp (nome no Leadbox/Meta)
WHATSAPP_TEMPLATE = "manutencao"  # 3 params: {{1}}=nome, {{2}}=equipamento, {{3}}=endereço

# Texto legível para salvar no histórico (conversation_history) — não enviado ao WhatsApp
TEMPLATE_HISTORICO = (
    "Olá, {nome}!\n\n"
    "Está chegando a hora da manutenção preventiva do seu ar-condicionado.\n\n"
    "Equipamento: {equipamento}\n"
    "Endereço: {endereco}\n\n"
    "A manutenção é gratuita e já está inclusa no seu contrato. "
    "Quer agendar? Me responde com um dia e horário de preferência."
)


def buscar_contratos_d7(hoje: date) -> list:
    """Busca contratos com manutenção prevista para daqui 7 dias."""
    supabase = get_supabase()
    if not supabase:
        return []

    data_alvo = (hoje + timedelta(days=7)).isoformat()

    try:
        result = supabase.table(TABLE_CONTRACT_DETAILS).select(
            "id, customer_id, locatario_nome, locatario_telefone, "
            "equipamentos, endereco_instalacao, proxima_manutencao, "
            "maintenance_status"
        ).eq(
            "proxima_manutencao", data_alvo
        ).is_(
            "deleted_at", "null"
        ).execute()

        if not result.data:
            return []

        elegiveis = []
        for contrato in result.data:
            # Pular se já notificado
            if contrato.get("maintenance_status") == "notified":
                continue

            # Buscar telefone: primeiro do contrato, depois do cliente Asaas
            phone = contrato.get("locatario_telefone")
            if not phone:
                customer_id = contrato.get("customer_id")
                if customer_id:
                    cliente = supabase.table(TABLE_ASAAS_CLIENTES).select(
                        "mobile_phone"
                    ).eq("id", customer_id).limit(1).execute()
                    if cliente.data:
                        phone = cliente.data[0].get("mobile_phone")

            if not phone or len(phone) < 10:
                logger.warning(f"[MANUTENCAO] Contrato {contrato['id']} sem telefone válido")
                continue

            # Formatar equipamento
            equipamentos = contrato.get("equipamentos") or []
            if equipamentos and isinstance(equipamentos, list):
                eq = equipamentos[0]
                equipamento_str = f"{eq.get('marca', '?')} {eq.get('btus', '?')} BTUs"
                if len(equipamentos) > 1:
                    equipamento_str += f" (+{len(equipamentos)-1} equipamento(s))"
            else:
                equipamento_str = "Ar-condicionado"

            nome = contrato.get("locatario_nome", "Cliente")
            primeiro_nome = nome.split()[0] if nome else "Cliente"
            endereco = contrato.get("endereco_instalacao", "Endereço não informado")

            # Params na ordem do template Meta: {{1}}=nome, {{2}}=equipamento, {{3}}=endereço
            template_params = [primeiro_nome, equipamento_str, endereco]

            # Texto legível para histórico interno (não enviado ao WhatsApp)
            message = TEMPLATE_HISTORICO.format(
                nome=primeiro_nome,
                equipamento=equipamento_str,
                endereco=endereco,
            )

            elegiveis.append({
                "phone": phone,
                "message": message,
                "contract_id": contrato["id"],
                "context_type": "manutencao_preventiva",
                "template_params": template_params,
                "nome": nome,
            })

        return elegiveis

    except Exception as e:
        logger.exception("[MANUTENCAO] Falha ao buscar contratos")
        return []


async def run_manutencao():
    """Entry point do job de manutenção."""
    from infra.redis import get_redis_service

    hoje = date.today()
    weekday = hoje.weekday()
    if weekday >= 5:
        logger.info("[MANUTENCAO] Fim de semana, pulando")
        return

    from core.feriados import eh_feriado
    feriado = eh_feriado(hoje)
    if feriado:
        logger.info(f"[MANUTENCAO] Feriado ({feriado}), pulando")
        return

    redis = await get_redis_service()

    lock_key = "lock:manutencao_job"
    if not await redis.client.set(lock_key, "1", nx=True, ex=3600):
        logger.info("[MANUTENCAO] Já em execução")
        return

    try:
        logger.info("[MANUTENCAO] Iniciando")
        elegiveis = buscar_contratos_d7(hoje)
        logger.info(f"[MANUTENCAO] {len(elegiveis)} contratos para notificar")

        enviados = 0
        erros = 0

        for item in elegiveis:
            try:
                ok = await _processar_notificacao(item, redis)
                if ok:
                    enviados += 1
            except Exception as e:
                erros += 1
                logger.error(f"[MANUTENCAO] Erro: {e}", exc_info=True)
                from infra.incidentes import registrar_incidente
                registrar_incidente(item.get("phone", "?"), "manutencao_erro", str(e)[:300], {"contract_id": item.get("contract_id")})

        logger.info(f"[MANUTENCAO] Concluído: enviados={enviados} erros={erros}")

    finally:
        await redis.client.delete(lock_key)


async def _processar_notificacao(item: dict, redis) -> bool:
    """Processa uma notificação de manutenção."""
    from infra.event_logger import log_event

    phone = item["phone"]
    message = item["message"]
    contract_id = item["contract_id"]
    context_type = item["context_type"]

    if await redis.is_paused(phone):
        logger.info(f"[MANUTENCAO:{phone}] Pausado, adiando")
        return False

    # Anti-duplicata
    dedup_key = f"dispatch:{phone}:{context_type}:{contract_id}:{date.today().isoformat()}"
    if await redis.client.exists(dedup_key):
        logger.info(f"[MANUTENCAO:{phone}] Já notificou hoje")
        return False

    # Salvar contexto ANTES de enviar
    supabase = get_supabase()
    if not supabase:
        return False

    now = datetime.now(timezone.utc).isoformat()
    clean_phone = "".join(filter(str.isdigit, phone))

    # Buscar lead
    lead = None
    for tel in [clean_phone, clean_phone[2:] if clean_phone.startswith("55") else f"55{clean_phone}"]:
        result = supabase.table(TABLE_LEADS).select(
            "id, conversation_history"
        ).eq("telefone", tel).limit(1).execute()
        if result.data:
            lead = result.data[0]
            break

    if not lead:
        from infra.nodes_supabase import upsert_lead
        lead_id = upsert_lead(clean_phone, nome=item.get("nome"))
        if lead_id:
            init_history = {"messages": [{
                "role": "user",
                "content": f"[Notificação de manutenção preventiva recebida - contrato {contract_id}]",
                "timestamp": now,
            }]}
            supabase.table(TABLE_LEADS).update({
                "conversation_history": init_history,
                "updated_at": now,
            }).eq("id", lead_id).execute()

            result = supabase.table(TABLE_LEADS).select(
                "id, conversation_history"
            ).eq("id", lead_id).limit(1).execute()
            if result.data:
                lead = result.data[0]

    if not lead:
        logger.warning(f"[MANUTENCAO:{phone}] Lead não encontrado/criado")
        return False

    # Salvar contexto
    history = lead.get("conversation_history") or {"messages": []}
    history["messages"].append({
        "role": "model",
        "content": message,
        "timestamp": now,
        "context": context_type,
        "contract_id": contract_id,
    })

    supabase.table(TABLE_LEADS).update({
        "conversation_history": history,
        "updated_at": now,
    }).eq("id", lead["id"]).execute()

    # Marcar contrato como notificado
    try:
        supabase.table(TABLE_CONTRACT_DETAILS).update({
            "maintenance_status": "notified",
            "notificacao_enviada_at": now,
        }).eq("id", contract_id).execute()
    except Exception as e:
        logger.warning(f"[MANUTENCAO:{phone}] Erro ao marcar contrato: {e}")

    # Enviar template via Leadbox (1 POST: Leadbox → Meta → WhatsApp)
    from infra.leadbox_client import enviar_template_leadbox

    tel_envio = clean_phone if clean_phone.startswith("55") else f"55{clean_phone}"

    from core.constants import QUEUE_ATENDIMENTO, USER_NATHALIA
    if not enviar_template_leadbox(
        tel_envio, WHATSAPP_TEMPLATE, item["template_params"],
        queue_id=QUEUE_ATENDIMENTO, user_id=USER_NATHALIA,
    ):
        logger.error(f"[MANUTENCAO:{phone}] Leadbox erro ao enviar template")
        await redis.client.set(dedup_key, "1", ex=86400)
        return False

    await redis.client.set(dedup_key, "1", ex=86400)
    logger.info(f"[MANUTENCAO:{phone}] Notificação D-7 enviada (contrato={contract_id})")
    log_event("manutencao_sent", phone, contract_id=contract_id)
    return True


if __name__ == "__main__":
    asyncio.run(run_manutencao())
