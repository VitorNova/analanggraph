"""Job de Billing — Disparos automáticos de cobrança.

Busca cobranças PENDING/OVERDUE no Supabase (sincronizado do Asaas),
aplica régua de dias úteis, e envia cobrança via WhatsApp.

Salva contexto no histórico ANTES de enviar (se envio falhar, contexto já está).

Uso:
    python jobs/billing_job.py            # Roda manualmente
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
from infra.incidentes import registrar_incidente
from core.constants import TABLE_LEADS, TABLE_ASAAS_CLIENTES, TABLE_ASAAS_COBRANCAS

# UUID do agente Ana na tabela agents (usado em asaas_cobrancas.agent_id)
ANA_AGENT_UUID = "14e6e5ce-4627-4e38-aac8-f0191669ff53"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Régua de cobrança: offsets em dias úteis onde envia
SCHEDULE = [0, 1, 3, 5, 7, 10, 15]
SCHEDULE_EXTENDED_INTERVAL = 5  # A cada 5 dias úteis após max(SCHEDULE) — modo log-only por enquanto

# Mapeamento: tipo de disparo → template oficial WhatsApp (nome no Meta)
# Params são sempre: [nome, valor, vencimento, link] na ordem {{1}}..{{4}}
WHATSAPP_TEMPLATES = {
    "due_date": "diavencimento",   # Vence hoje — 4 params
    "overdue": "cobranca",         # Vencido — 4 params
}

# Texto legível para salvar no histórico (conversation_history)
# Não é enviado ao WhatsApp — só para contexto interno
TEMPLATES_HISTORICO = {
    "due_date": (
        "Olá, {nome}. Sua mensalidade de R$ {valor} vence hoje ({vencimento}).\n"
        "Link para pagamento: {link}\n"
        "Se já efetuou o pagamento, desconsidere esta mensagem."
    ),
    "overdue": (
        "Olá, {nome}. Sua mensalidade de R$ {valor} com vencimento em {vencimento} encontra-se em aberto.\n"
        "Para regularizar, acesse: {link}\n"
        "Se já efetuou o pagamento, desconsidere esta mensagem.\n"
        "Em caso de dúvida, responda aqui."
    ),
}


def count_business_days(from_date: date, to_date: date) -> int:
    """Conta dias úteis entre duas datas (descontando feriados)."""
    from core.feriados import eh_feriado

    if from_date == to_date:
        return 0
    sign = 1 if to_date > from_date else -1
    start = min(from_date, to_date)
    end = max(from_date, to_date)
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5 and not eh_feriado(current):
            count += 1
        current += timedelta(days=1)
    return count * sign


def get_template_key(offset: int) -> str:
    """Retorna template_key baseado no offset."""
    return "due_date" if offset == 0 else "overdue"


def buscar_elegiveis(hoje: date) -> list:
    """Busca cobranças elegíveis para disparo hoje."""
    supabase = get_supabase()
    if not supabase:
        logger.error("[BILLING] Supabase indisponível — impossível buscar cobranças")
        registrar_incidente("BILLING_SYSTEM", "billing_supabase_fora", "get_supabase() retornou None")
        return []

    # Janela dinâmica: max(SCHEDULE) * 1.6 dias corridos (cobre feriados)
    _janela_dias = int(max(SCHEDULE) * 1.6)
    min_date = (hoje - timedelta(days=_janela_dias)).isoformat()
    max_date = (hoje + timedelta(days=5)).isoformat()

    try:
        # Cobranças PENDING (vencimento próximo)
        pending = supabase.table(TABLE_ASAAS_COBRANCAS).select(
            "id, customer_id, value, due_date, status, invoice_url"
        ).in_(
            "status", ["PENDING", "OVERDUE"]
        ).is_(
            "deleted_at", "null"
        ).gte("due_date", min_date).lte("due_date", max_date).execute()

        if not pending.data:
            return []

        # Buscar clientes para cada cobrança
        customer_ids = list({c["customer_id"] for c in pending.data})
        clientes = supabase.table(TABLE_ASAAS_CLIENTES).select(
            "id, name, mobile_phone, cpf_cnpj"
        ).in_("id", customer_ids).is_("deleted_at", "null").execute()

        cliente_map = {c["id"]: c for c in (clientes.data or [])}

        elegiveis = []
        skips = {
            "cliente_ausente": 0,
            "telefone_invalido": 0,
            "fora_do_schedule": 0,
            "vencimento_fds": 0,
            "sem_link": 0,
            "schedule_estendido_candidato": 0,
        }

        for cob in pending.data:
            cliente = cliente_map.get(cob["customer_id"])
            if not cliente:
                skips["cliente_ausente"] += 1
                logger.warning(
                    f"[BILLING] Cobrança {cob['id']} — cliente {cob['customer_id']} não encontrado em asaas_clientes"
                )
                registrar_incidente(
                    cob.get("customer_id", "?"),
                    "billing_cliente_ausente",
                    f"Cobrança {cob['id']} com customer_id={cob['customer_id']} sem registro em asaas_clientes"
                )
                continue

            phone = cliente.get("mobile_phone", "")
            if not phone or len(phone) < 10:
                skips["telefone_invalido"] += 1
                logger.warning(
                    f"[BILLING] Cobrança {cob['id']} — telefone inválido '{phone}' para cliente {cob['customer_id']}"
                )
                continue

            due = date.fromisoformat(cob["due_date"][:10])
            offset = count_business_days(due, hoje)

            if offset not in SCHEDULE:
                # Schedule estendido: offset > max e múltiplo de 5 → log-only (Fase 2)
                if offset > max(SCHEDULE) and (offset - max(SCHEDULE)) % SCHEDULE_EXTENDED_INTERVAL == 0:
                    skips["schedule_estendido_candidato"] += 1
                    logger.info(
                        f"[BILLING] Cobrança {cob['id']} offset={offset} — candidata a schedule estendido (log-only)"
                    )
                else:
                    skips["fora_do_schedule"] += 1
                continue

            # Vencimento no fim de semana: offset=0 mas due != hoje
            # Não disparar "vence hoje" — será capturado como overdue na segunda
            if offset == 0 and due != hoje:
                skips["vencimento_fds"] += 1
                continue

            template_key = get_template_key(offset)

            link = cob.get("invoice_url") or ""
            if not link:
                skips["sem_link"] += 1
                logger.warning(f"[BILLING] Cobrança {cob['id']} sem link de pagamento, pulando")
                registrar_incidente(
                    phone or cob.get("customer_id", "?"),
                    "billing_sem_link",
                    f"Cobrança {cob['id']} sem invoice_url (offset={offset})"
                )
                continue

            nome = cliente.get("name", "Cliente")
            valor = f"{cob['value']:.2f}"
            vencimento = due.strftime("%d/%m/%Y")

            # Texto legível para histórico interno (não enviado ao WhatsApp)
            message = TEMPLATES_HISTORICO[template_key].format(
                nome=nome, valor=valor, vencimento=vencimento, link=link,
            )

            elegiveis.append({
                "phone": phone,
                "message": message,
                "reference_id": cob["id"],
                "context_type": "billing",
                "template_params": [nome, valor, vencimento, link],
                "template_key": template_key,
                "offset": offset,
                "nome": nome,
            })

        # Sumário de filtro
        total_cobrancas = len(pending.data)
        total_skips = sum(skips.values())
        logger.info(
            f"[BILLING] Filtro: {total_cobrancas} cobranças → {len(elegiveis)} elegíveis "
            f"({total_skips} filtradas: {skips})"
        )

        return elegiveis

    except Exception as e:
        logger.exception("[BILLING] Falha ao buscar elegíveis")
        return []


async def run_billing():
    """Entry point do billing job."""
    from infra.redis import get_redis_service

    hoje = date.today()
    weekday = hoje.weekday()
    if weekday >= 5:  # sáb/dom
        logger.info("[BILLING] Fim de semana, pulando")
        return

    from core.feriados import eh_feriado
    feriado = eh_feriado(hoje)
    if feriado:
        logger.info(f"[BILLING] Feriado ({feriado}), pulando")
        return

    redis = await get_redis_service()

    # Lock Redis
    lock_key = "lock:billing_job"
    if not await redis.client.set(lock_key, "1", nx=True, ex=3600):
        logger.info("[BILLING] Já em execução")
        return

    try:
        # Heartbeat check: alertar se último heartbeat > 49h
        last_hb = await redis.client.get("heartbeat:billing_job")
        if last_hb:
            if isinstance(last_hb, bytes):
                last_hb = last_hb.decode()
            try:
                last_dt = datetime.fromisoformat(last_hb)
                gap_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if gap_hours > 49:
                    logger.error(f"[BILLING] ALERTA: último heartbeat há {gap_hours:.0f}h — job pode ter falhado")
                    registrar_incidente(
                        "BILLING_SYSTEM", "billing_heartbeat_gap",
                        f"Último heartbeat há {gap_hours:.0f}h. Job pode não ter rodado."
                    )
            except (ValueError, TypeError):
                pass

        logger.info("[BILLING] Iniciando")
        elegiveis = buscar_elegiveis(hoje)
        logger.info(f"[BILLING] {len(elegiveis)} elegíveis")

        # R6: zero elegíveis com cobranças no banco = anomalia
        if len(elegiveis) == 0:
            sb = get_supabase()
            if sb:
                try:
                    check = sb.table(TABLE_ASAAS_COBRANCAS).select(
                        "id", count="exact"
                    ).in_(
                        "status", ["PENDING", "OVERDUE"]
                    ).is_("deleted_at", "null").execute()
                    total_no_banco = check.count or 0
                    if total_no_banco > 0:
                        logger.warning(
                            f"[BILLING] 0 elegíveis mas {total_no_banco} cobranças PENDING/OVERDUE no banco"
                        )
                        registrar_incidente(
                            "BILLING_SYSTEM",
                            "billing_zero_elegiveis",
                            f"0 elegíveis em {hoje.isoformat()} mas {total_no_banco} cobranças no banco. "
                            f"Verificar filtros (janela, SCHEDULE, telefone, invoice_url)."
                        )
                    else:
                        logger.info("[BILLING] 0 elegíveis — banco sem cobranças pendentes (normal)")
                except Exception as e:
                    logger.warning(f"[BILLING] Falha ao verificar cobranças no banco: {e}")

        enviados = 0
        erros = 0

        for item in elegiveis:
            try:
                ok = await _processar_disparo(item, redis)
                if ok:
                    enviados += 1
            except Exception as e:
                erros += 1
                logger.error(f"[BILLING] Erro: {e}", exc_info=True)
                registrar_incidente(item.get("phone", "?"), "billing_erro", str(e)[:300])

        logger.info(f"[BILLING] Concluído: enviados={enviados} erros={erros}")

    finally:
        # Heartbeat: gravar timestamp para detectar se job parou de rodar
        try:
            await redis.client.set(
                "heartbeat:billing_job",
                datetime.now(timezone.utc).isoformat(),
                ex=90000,  # TTL 25h
            )
        except Exception:
            pass
        await redis.client.delete(lock_key)


async def _processar_disparo(item: dict, redis) -> bool:
    """Processa um disparo: anti-duplicata -> salvar contexto -> enviar."""
    import json as _json
    from infra.event_logger import log_event
    from infra.flow_tracer import start_execution, trace_node, finish_execution

    phone = item["phone"]
    message = item["message"]
    reference_id = item["reference_id"]
    context_type = item["context_type"]

    # -- Flow tracer: 1 execução por cobrança --
    start_execution(
        phone,
        input_preview=f"R$ {item.get('template_params', ['',''])[1]} venc. {item.get('template_params', ['','',''])[2]}",
        trigger="billing_job",
        nome=item.get("nome", ""),
    )

    # Node: buscar_elegiveis (dados da cobrança)
    async with trace_node("buscar_elegiveis", input_data=_json.dumps({
        "cobranca_id": reference_id,
        "template": item.get("template_key"),
        "offset_dias": item.get("offset"),
        "valor": item.get("template_params", ["", ""])[1] if len(item.get("template_params", [])) > 1 else "",
        "vencimento": item.get("template_params", ["", "", ""])[2] if len(item.get("template_params", [])) > 2 else "",
    }, ensure_ascii=False)) as nd_el:
        nd_el["output_data"] = _json.dumps({
            "nome": item.get("nome"),
            "telefone": phone,
            "template_key": item.get("template_key"),
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
        logger.info(f"[BILLING:{phone}] Pausado, adiando")
        log_event("billing_skipped", phone, reason="paused")
        await finish_execution("completed", "pausado")
        return False

    # Node: verif_snooze
    async with trace_node("verif_snooze", input_data=_json.dumps({"telefone": clean_phone}, ensure_ascii=False)) as nd_sn:
        snoozed = await redis.is_snoozed(clean_phone, "billing")
        snooze_until = None
        snooze_db = None
        if snoozed:
            snooze_until = await redis.snooze_get(clean_phone, "billing")
            nd_sn["output_data"] = _json.dumps({"snoozed": True, "ate": snooze_until, "fonte": "redis"}, ensure_ascii=False)
        else:
            # Fallback Supabase
            try:
                _sb = get_supabase()
                if _sb:
                    _lead_snooze = _sb.table(TABLE_LEADS).select(
                        "billing_snooze_until"
                    ).eq("telefone", clean_phone).limit(1).execute()
                    if _lead_snooze.data:
                        snooze_db = _lead_snooze.data[0].get("billing_snooze_until")
                        if snooze_db and date.fromisoformat(snooze_db) >= date.today():
                            snoozed = True
                            await redis.snooze_set(clean_phone, snooze_db)
                            nd_sn["output_data"] = _json.dumps({"snoozed": True, "ate": snooze_db, "fonte": "supabase"}, ensure_ascii=False)
                        elif snooze_db:
                            _sb.table(TABLE_LEADS).update({"billing_snooze_until": None}).eq("telefone", clean_phone).execute()
                            nd_sn["output_data"] = _json.dumps({"snoozed": False, "expirado": snooze_db}, ensure_ascii=False)
                        else:
                            nd_sn["output_data"] = _json.dumps({"snoozed": False}, ensure_ascii=False)
                    else:
                        nd_sn["output_data"] = _json.dumps({"snoozed": False}, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"[BILLING:{clean_phone}] Snooze DB check falhou — fail-safe, pulando: {e}")
                nd_sn["status"] = "error"
                nd_sn["error_message"] = str(e)[:300]
                nd_sn["output_data"] = _json.dumps({"snoozed": True, "motivo": "fail-safe db error"}, ensure_ascii=False)
                await finish_execution("completed", "snooze_db_error")
                return False
    if snoozed:
        logger.info(f"[BILLING:{clean_phone}] Snooze ativo até {snooze_until or snooze_db}, pulando")
        log_event("billing_skipped", clean_phone, reason="snoozed", until=snooze_until or snooze_db)
        await finish_execution("completed", "snoozed")
        return False

    # Node: verif_duplicata
    dedup_key = f"dispatch:{clean_phone}:{context_type}:{reference_id}:{date.today().isoformat()}"
    async with trace_node("verif_duplicata", input_data=_json.dumps({"chave": dedup_key}, ensure_ascii=False)) as nd_dd:
        exists = await redis.client.exists(dedup_key)
        nd_dd["output_data"] = _json.dumps({"duplicata": bool(exists)}, ensure_ascii=False)
    if exists:
        logger.info(f"[BILLING:{phone}] Já enviou hoje")
        await finish_execution("completed", "duplicata")
        return False

    # ORDEM CRÍTICA: salvar contexto ANTES de enviar
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
            result = supabase.table(TABLE_LEADS).select(
                "id, conversation_history"
            ).eq("id", lead_id).limit(1).execute()
            if result.data:
                lead = result.data[0]

    if not lead:
        logger.warning(f"[BILLING:{phone}] Lead não encontrado/criado")
        await finish_execution("error", error_msg="lead_nao_encontrado")
        return False

    # Node: salvar_contexto
    async with trace_node("salvar_contexto", input_data=_json.dumps({
        "lead_id": lead["id"], "contexto": context_type, "referencia": reference_id,
    }, ensure_ascii=False)) as nd_ctx:
        history = lead.get("conversation_history") or {"messages": []}
        history["messages"].append({
            "role": "model", "content": message, "timestamp": now,
            "context": context_type, "reference_id": reference_id,
        })
        supabase.table(TABLE_LEADS).update({
            "conversation_history": history, "updated_at": now,
        }).eq("id", lead["id"]).execute()
        nd_ctx["output_data"] = _json.dumps({"salvo": True, "mensagens_total": len(history["messages"])}, ensure_ascii=False)

    # Node: enviar_template
    from infra.leadbox_client import enviar_template_leadbox
    from core.constants import QUEUE_BILLING, USER_IA

    tel_envio = clean_phone if clean_phone.startswith("55") else f"55{clean_phone}"
    wa_template = WHATSAPP_TEMPLATES[item["template_key"]]
    template_params = item["template_params"]

    async with trace_node("enviar_template", input_data=_json.dumps({
        "telefone": tel_envio, "template": wa_template,
        "params": template_params, "fila": QUEUE_BILLING, "usuario": USER_IA,
    }, ensure_ascii=False)) as nd_env:
        ok = enviar_template_leadbox(
            tel_envio, wa_template, template_params,
            body_texto=message, queue_id=QUEUE_BILLING, user_id=USER_IA,
        )
        if ok:
            nd_env["output_data"] = _json.dumps({"status": "enviado", "template": wa_template}, ensure_ascii=False)
        else:
            nd_env["status"] = "error"
            nd_env["error_message"] = f"Template {wa_template} falhou"
            nd_env["output_data"] = _json.dumps({"status": "falhou", "template": wa_template}, ensure_ascii=False)

    if not ok:
        logger.error(f"[BILLING:{phone}] Falha ao enviar template '{wa_template}'")
        log_event("billing_error", phone, reason="template_failed", template=item.get("template_key"))
        registrar_incidente(phone, "billing_envio_falhou", f"Template {wa_template} falhou para cobrança {reference_id}")
        try:
            history["messages"][-1]["delivery_failed"] = True
            supabase.table(TABLE_LEADS).update({"conversation_history": history}).eq("id", lead["id"]).execute()
        except Exception:
            pass
        await finish_execution("error", error_msg=f"template_failed:{wa_template}")
        return False

    # Node: marcar_cobranca
    async with trace_node("marcar_cobranca", input_data=_json.dumps({
        "cobranca_id": reference_id, "template_key": item["template_key"],
    }, ensure_ascii=False)) as nd_mc:
        try:
            existing = supabase.table(TABLE_ASAAS_COBRANCAS).select(
                "ia_total_notificacoes"
            ).eq("id", reference_id).eq("agent_id", ANA_AGENT_UUID).limit(1).execute()
            total = (existing.data[0].get("ia_total_notificacoes") or 0) + 1 if existing.data else 1

            supabase.table(TABLE_ASAAS_COBRANCAS).update({
                "ia_cobrou": True, "ia_cobrou_at": now,
                "ia_ultimo_step": item["template_key"], "ia_total_notificacoes": total,
            }).eq("id", reference_id).eq("agent_id", ANA_AGENT_UUID).execute()
            nd_mc["output_data"] = _json.dumps({"ia_cobrou": True, "notificacoes": total}, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[BILLING:{phone}] Falha ao marcar ia_cobrou: {e}")
            nd_mc["output_data"] = _json.dumps({"ia_cobrou": False, "erro": str(e)[:200]}, ensure_ascii=False)

    # Marcar anti-duplicata (24h)
    await redis.client.set(dedup_key, "1", ex=86400)
    logger.info(f"[BILLING:{phone}] Enviado ({item['template_key']}, offset={item['offset']})")
    log_event("billing_sent", phone, template=item.get("template_key"), offset=item.get("offset"), ref=reference_id)
    await finish_execution("completed", output_preview=f"Enviado {wa_template} para {clean_phone}")
    return True


if __name__ == "__main__":
    asyncio.run(run_billing())
