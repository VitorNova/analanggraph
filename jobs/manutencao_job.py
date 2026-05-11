"""Job de Manutenção Preventiva.

Busca contratos com proxima_manutencao entre hoje e hoje+7 dias
e envia lembrete via WhatsApp com dados do equipamento.
Também recupera contratos atrasados (até 30 dias, cap 5/dia).

Salva contexto "manutencao_preventiva" ou "manutencao_atrasada"
no histórico para que o context_detector diferencie o cenário.

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


def buscar_contratos_elegiveis(hoje: date) -> list:
    """Busca contratos com manutenção prevista entre hoje e hoje+7 dias."""
    supabase = get_supabase()
    if not supabase:
        return []

    data_inicio = hoje.isoformat()
    data_fim = (hoje + timedelta(days=7)).isoformat()

    try:
        result = supabase.table(TABLE_CONTRACT_DETAILS).select(
            "id, customer_id, locatario_nome, locatario_telefone, "
            "equipamentos, endereco_instalacao, proxima_manutencao, "
            "maintenance_status"
        ).gte(
            "proxima_manutencao", data_inicio
        ).lte(
            "proxima_manutencao", data_fim
        ).neq(
            "maintenance_status", "notified"
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
                marca = eq.get('marca') or eq.get('modelo') or 'Split'
                btus = eq.get('btus') or '?'
                equipamento_str = f"{marca} {btus} BTUs"
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


def buscar_contratos_atrasados(hoje: date, max_dias: int = 30, limite: int = 5) -> list:
    """Busca contratos com manutenção atrasada (data passada, não notificados).

    Recupera contratos que o job perdeu por fim de semana, feriado ou erro.
    Limita a `limite` por execução para não enviar spam.
    """
    supabase = get_supabase()
    if not supabase:
        return []

    data_limite = (hoje - timedelta(days=max_dias)).isoformat()

    try:
        result = supabase.table(TABLE_CONTRACT_DETAILS).select(
            "id, customer_id, locatario_nome, locatario_telefone, "
            "equipamentos, endereco_instalacao, proxima_manutencao, "
            "maintenance_status"
        ).lt(
            "proxima_manutencao", hoje.isoformat()
        ).gte(
            "proxima_manutencao", data_limite
        ).neq(
            "maintenance_status", "notified"
        ).neq(
            "maintenance_status", "done"
        ).is_(
            "deleted_at", "null"
        ).order(
            "proxima_manutencao", desc=True
        ).limit(20).execute()

        if not result.data:
            return []

        # Reusar a mesma lógica de formatação de buscar_contratos_elegiveis
        # mas com context_type diferenciado e dedup por customer_id
        vistos = set()
        elegiveis = []

        for contrato in result.data:
            customer_id = contrato.get("customer_id")
            if customer_id in vistos:
                continue

            phone = contrato.get("locatario_telefone")
            if not phone:
                if customer_id:
                    cliente = supabase.table(TABLE_ASAAS_CLIENTES).select(
                        "mobile_phone"
                    ).eq("id", customer_id).limit(1).execute()
                    if cliente.data:
                        phone = cliente.data[0].get("mobile_phone")

            if not phone or len(phone) < 10:
                logger.warning(f"[MANUTENCAO] Contrato atrasado {contrato['id']} sem telefone válido")
                continue

            equipamentos = contrato.get("equipamentos") or []
            if equipamentos and isinstance(equipamentos, list):
                eq = equipamentos[0]
                marca = eq.get('marca') or eq.get('modelo') or 'Split'
                btus = eq.get('btus') or '?'
                equipamento_str = f"{marca} {btus} BTUs"
                if len(equipamentos) > 1:
                    equipamento_str += f" (+{len(equipamentos)-1} equipamento(s))"
            else:
                equipamento_str = "Ar-condicionado"

            nome = contrato.get("locatario_nome", "Cliente")
            primeiro_nome = nome.split()[0] if nome else "Cliente"
            endereco = contrato.get("endereco_instalacao", "Endereço não informado")

            template_params = [primeiro_nome, equipamento_str, endereco]
            message = TEMPLATE_HISTORICO.format(
                nome=primeiro_nome,
                equipamento=equipamento_str,
                endereco=endereco,
            )

            vistos.add(customer_id)
            elegiveis.append({
                "phone": phone,
                "message": message,
                "contract_id": contrato["id"],
                "context_type": "manutencao_atrasada",
                "template_params": template_params,
                "nome": nome,
            })

            if len(elegiveis) >= limite:
                break

        return elegiveis

    except Exception as e:
        logger.exception("[MANUTENCAO] Falha ao buscar contratos atrasados")
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
        # P7: heartbeat check — alertar se último heartbeat > 49h
        last_hb = await redis.client.get("heartbeat:manutencao_job")
        if last_hb:
            if isinstance(last_hb, bytes):
                last_hb = last_hb.decode()
            try:
                last_dt = datetime.fromisoformat(last_hb)
                gap_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if gap_hours > 49:
                    logger.error(f"[MANUTENCAO] ALERTA: último heartbeat há {gap_hours:.0f}h — job pode ter falhado")
                    from infra.incidentes import registrar_incidente
                    registrar_incidente(
                        "MANUTENCAO_SYSTEM", "manutencao_heartbeat_gap",
                        f"Último heartbeat há {gap_hours:.0f}h. Job pode não ter rodado."
                    )
            except (ValueError, TypeError):
                pass

        logger.info("[MANUTENCAO] Iniciando")
        elegiveis = buscar_contratos_elegiveis(hoje)
        atrasados = buscar_contratos_atrasados(hoje, max_dias=30, limite=5)
        if atrasados:
            logger.info(f"[MANUTENCAO] {len(atrasados)} contratos atrasados recuperados")
            elegiveis.extend(atrasados)
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
        # P7: gravar heartbeat para detectar se job parou de rodar
        try:
            await redis.client.set(
                "heartbeat:manutencao_job",
                datetime.now(timezone.utc).isoformat(),
                ex=90000,  # TTL 25h
            )
        except Exception:
            pass
        await redis.client.delete(lock_key)


async def _processar_notificacao(item: dict, redis) -> bool:
    """Processa uma notificação de manutenção."""
    import json as _json
    from infra.event_logger import log_event
    from infra.flow_tracer import start_execution, trace_node, finish_execution

    phone = item["phone"]
    message = item["message"]
    contract_id = item["contract_id"]
    context_type = item["context_type"]

    # -- Flow tracer: 1 execução por contrato --
    start_execution(
        phone,
        input_preview=f"Contrato {contract_id} — {context_type}",
        trigger="manutencao_job",
        nome=item.get("nome", ""),
    )

    # Node: buscar_contratos (dados do contrato)
    async with trace_node("buscar_contratos", input_data=_json.dumps({
        "contrato_id": contract_id, "tipo": context_type,
        "equipamento": item.get("template_params", ["", ""])[1] if len(item.get("template_params", [])) > 1 else "",
        "endereco": item.get("template_params", ["", "", ""])[2] if len(item.get("template_params", [])) > 2 else "",
    }, ensure_ascii=False)) as nd_ct:
        nd_ct["output_data"] = _json.dumps({
            "nome": item.get("nome"), "telefone": phone,
            "params": item.get("template_params", []),
        }, ensure_ascii=False)

    # Node: normalizar_tel
    async with trace_node("normalizar_tel", input_data=_json.dumps({"telefone_original": phone}, ensure_ascii=False)) as nd_tel:
        clean_phone = "".join(filter(str.isdigit, phone))
        if len(clean_phone) in (10, 11):
            clean_phone = "55" + clean_phone
        nd_tel["output_data"] = _json.dumps({"telefone_normalizado": clean_phone}, ensure_ascii=False)

    # Node: verif_pausa
    async with trace_node("verif_pausa", input_data=_json.dumps({"telefone": clean_phone}, ensure_ascii=False)) as nd_p:
        paused = await redis.is_paused(clean_phone)
        nd_p["output_data"] = _json.dumps({"pausado": paused}, ensure_ascii=False)
    if paused:
        logger.info(f"[MANUTENCAO:{clean_phone}] Pausado, adiando")
        log_event("manutencao_skipped", clean_phone, reason="paused")
        await finish_execution("completed", "pausado")
        return False

    # Node: verif_duplicata
    dedup_key = f"dispatch:{clean_phone}:{context_type}:{contract_id}:{date.today().isoformat()}"
    async with trace_node("verif_duplicata", input_data=_json.dumps({"chave": dedup_key}, ensure_ascii=False)) as nd_dd:
        exists = await redis.client.exists(dedup_key)
        nd_dd["output_data"] = _json.dumps({"duplicata": bool(exists)}, ensure_ascii=False)
    if exists:
        logger.info(f"[MANUTENCAO:{clean_phone}] Já notificou hoje")
        log_event("manutencao_skipped", clean_phone, reason="dedup")
        await finish_execution("completed", "duplicata")
        return False

    # Salvar contexto ANTES de enviar
    supabase = get_supabase()
    if not supabase:
        await finish_execution("error", error_msg="supabase_indisponivel")
        return False

    now = datetime.now(timezone.utc).isoformat()

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
                "conversation_history": init_history, "updated_at": now,
            }).eq("id", lead_id).execute()
            result = supabase.table(TABLE_LEADS).select(
                "id, conversation_history"
            ).eq("id", lead_id).limit(1).execute()
            if result.data:
                lead = result.data[0]

    if not lead:
        logger.warning(f"[MANUTENCAO:{clean_phone}] Lead não encontrado/criado")
        await finish_execution("error", error_msg="lead_nao_encontrado")
        return False

    # Node: salvar_contexto
    async with trace_node("salvar_contexto", input_data=_json.dumps({
        "lead_id": lead["id"], "contexto": context_type, "contrato": contract_id,
    }, ensure_ascii=False)) as nd_ctx:
        history = lead.get("conversation_history") or {"messages": []}
        history["messages"].append({
            "role": "model", "content": message, "timestamp": now,
            "context": context_type, "contract_id": contract_id,
        })
        supabase.table(TABLE_LEADS).update({
            "conversation_history": history, "updated_at": now,
        }).eq("id", lead["id"]).execute()
        nd_ctx["output_data"] = _json.dumps({"salvo": True, "mensagens_total": len(history["messages"])}, ensure_ascii=False)

    # Node: enviar_template
    from infra.leadbox_client import enviar_template_leadbox
    tel_envio = clean_phone if clean_phone.startswith("55") else f"55{clean_phone}"
    from core.constants import QUEUE_MANUTENCAO, USER_IA

    async with trace_node("enviar_template", input_data=_json.dumps({
        "telefone": tel_envio, "template": WHATSAPP_TEMPLATE,
        "params": item["template_params"], "fila": QUEUE_MANUTENCAO, "usuario": USER_IA,
    }, ensure_ascii=False)) as nd_env:
        ok = enviar_template_leadbox(
            tel_envio, WHATSAPP_TEMPLATE, item["template_params"],
            queue_id=QUEUE_MANUTENCAO, user_id=USER_IA,
        )
        if ok:
            nd_env["output_data"] = _json.dumps({"status": "enviado", "template": WHATSAPP_TEMPLATE}, ensure_ascii=False)
        else:
            nd_env["status"] = "error"
            nd_env["error_message"] = f"Template {WHATSAPP_TEMPLATE} falhou"
            nd_env["output_data"] = _json.dumps({"status": "falhou"}, ensure_ascii=False)

    if not ok:
        logger.error(f"[MANUTENCAO:{clean_phone}] Leadbox erro ao enviar template")
        from infra.incidentes import registrar_incidente
        registrar_incidente(clean_phone, "manutencao_envio_falhou", f"Template {WHATSAPP_TEMPLATE} falhou para contrato {contract_id}")
        try:
            history["messages"][-1]["delivery_failed"] = True
            supabase.table(TABLE_LEADS).update({"conversation_history": history}).eq("id", lead["id"]).execute()
        except Exception:
            pass
        await finish_execution("error", error_msg=f"template_failed:{WHATSAPP_TEMPLATE}")
        return False

    # Node: marcar_notificado
    async with trace_node("marcar_notificado", input_data=_json.dumps({"contrato_id": contract_id}, ensure_ascii=False)) as nd_mn:
        try:
            supabase.table(TABLE_CONTRACT_DETAILS).update({
                "maintenance_status": "notified", "notificacao_enviada_at": now,
            }).eq("id", contract_id).execute()
            nd_mn["output_data"] = _json.dumps({"status": "notified", "contrato": contract_id}, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[MANUTENCAO:{clean_phone}] Erro ao marcar contrato: {e}")
            nd_mn["output_data"] = _json.dumps({"erro": str(e)[:200]}, ensure_ascii=False)

    await redis.client.set(dedup_key, "1", ex=86400)
    logger.info(f"[MANUTENCAO:{clean_phone}] Notificação enviada (contrato={contract_id})")
    log_event("manutencao_sent", clean_phone, contract_id=contract_id)
    await finish_execution("completed", output_preview=f"Enviado manutencao para {clean_phone}")
    return True


if __name__ == "__main__":
    asyncio.run(run_manutencao())
