"""
API endpoints para o Flow Dashboard.
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Query, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()

DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "ana-flow-2026")
TZ_MT = timezone(timedelta(hours=-4))


def _check_token(token: str):
    if token != DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="Token invalido")


# ── Definicoes estaticas dos grafos ──

FLOW_DEFINITIONS = {
    "webhook": {
        "nodes": [
            {"id": "webhook",           "label": "Webhook Leadbox",        "x": 400, "y": 0,    "icon": "webhook"},
            {"id": "buffer_9s",         "label": "Buffer 9s",              "x": 400, "y": 90,   "icon": "timer"},
            {"id": "pause_check",       "label": "Verif. Pausa",           "x": 400, "y": 180,  "icon": "pause"},
            {"id": "queue_check",       "label": "Verif. Fila",            "x": 400, "y": 270,  "icon": "queue"},
            {"id": "historico",         "label": "Historico",              "x": 400, "y": 360,  "icon": "history"},
            {"id": "transcricao",       "label": "Transcricao",            "x": 650, "y": 360,  "icon": "mic"},
            {"id": "context_detection", "label": "Detectar Contexto",      "x": 400, "y": 450,  "icon": "search"},
            {"id": "ai_agent",          "label": "Agente IA (Gemini)",     "x": 400, "y": 540,  "icon": "robot"},
            {"id": "tool_consultar",    "label": "Consultar Cliente",      "x": 150, "y": 640,  "icon": "lookup"},
            {"id": "tool_transferir",   "label": "Transferir Dept.",       "x": 400, "y": 640,  "icon": "transfer"},
            {"id": "tool_compromisso",  "label": "Registrar Compromisso",  "x": 650, "y": 640,  "icon": "calendar"},
            {"id": "guardrail",         "label": "Validacao",              "x": 400, "y": 740,  "icon": "shield"},
            {"id": "enviar_resposta",   "label": "Enviar Resposta",        "x": 400, "y": 830,  "icon": "send"},
            {"id": "salvar_historico",  "label": "Salvar Historico",       "x": 400, "y": 920,  "icon": "save"},
        ],
        "edges": [
            {"from": "webhook",           "to": "buffer_9s"},
            {"from": "buffer_9s",         "to": "pause_check"},
            {"from": "pause_check",       "to": "queue_check"},
            {"from": "queue_check",       "to": "historico"},
            {"from": "historico",         "to": "transcricao",       "type": "optional"},
            {"from": "historico",         "to": "context_detection"},
            {"from": "transcricao",       "to": "context_detection", "type": "optional"},
            {"from": "context_detection", "to": "ai_agent"},
            {"from": "ai_agent",          "to": "tool_consultar",    "type": "optional"},
            {"from": "ai_agent",          "to": "tool_transferir",   "type": "optional"},
            {"from": "ai_agent",          "to": "tool_compromisso",  "type": "optional"},
            {"from": "tool_consultar",    "to": "ai_agent",          "type": "loop"},
            {"from": "tool_transferir",   "to": "ai_agent",          "type": "loop"},
            {"from": "tool_compromisso",  "to": "ai_agent",          "type": "loop"},
            {"from": "ai_agent",          "to": "guardrail"},
            {"from": "guardrail",         "to": "enviar_resposta"},
            {"from": "enviar_resposta",   "to": "salvar_historico"},
        ],
    },
    "billing": {
        "nodes": [
            {"id": "buscar_elegiveis", "label": "Buscar Elegiveis",    "x": 400, "y": 50,  "icon": "search"},
            {"id": "normalizar_tel",   "label": "Normalizar Tel.",     "x": 400, "y": 150, "icon": "phone"},
            {"id": "verif_pausa",      "label": "Verif. Pausa",        "x": 400, "y": 250, "icon": "pause"},
            {"id": "verif_snooze",     "label": "Verif. Snooze",       "x": 400, "y": 350, "icon": "timer"},
            {"id": "verif_duplicata",  "label": "Verif. Duplicata",    "x": 400, "y": 450, "icon": "shield"},
            {"id": "salvar_contexto",  "label": "Salvar Contexto",     "x": 400, "y": 550, "icon": "save"},
            {"id": "enviar_template",  "label": "Enviar Template",     "x": 400, "y": 650, "icon": "send"},
            {"id": "marcar_cobranca",  "label": "Marcar Cobranca",     "x": 400, "y": 750, "icon": "check"},
        ],
        "edges": [
            {"from": "buscar_elegiveis", "to": "normalizar_tel"},
            {"from": "normalizar_tel",   "to": "verif_pausa"},
            {"from": "verif_pausa",      "to": "verif_snooze"},
            {"from": "verif_snooze",     "to": "verif_duplicata"},
            {"from": "verif_duplicata",  "to": "salvar_contexto"},
            {"from": "salvar_contexto",  "to": "enviar_template"},
            {"from": "enviar_template",  "to": "marcar_cobranca"},
        ],
    },
    "manutencao": {
        "nodes": [
            {"id": "buscar_contratos", "label": "Buscar Contratos",    "x": 400, "y": 50,  "icon": "search"},
            {"id": "normalizar_tel",   "label": "Normalizar Tel.",     "x": 400, "y": 150, "icon": "phone"},
            {"id": "verif_pausa",      "label": "Verif. Pausa",        "x": 400, "y": 250, "icon": "pause"},
            {"id": "verif_duplicata",  "label": "Verif. Duplicata",    "x": 400, "y": 350, "icon": "shield"},
            {"id": "salvar_contexto",  "label": "Salvar Contexto",     "x": 400, "y": 450, "icon": "save"},
            {"id": "enviar_template",  "label": "Enviar Template",     "x": 400, "y": 550, "icon": "send"},
            {"id": "marcar_notificado","label": "Marcar Notificado",   "x": 400, "y": 650, "icon": "check"},
        ],
        "edges": [
            {"from": "buscar_contratos", "to": "normalizar_tel"},
            {"from": "normalizar_tel",   "to": "verif_pausa"},
            {"from": "verif_pausa",      "to": "verif_duplicata"},
            {"from": "verif_duplicata",  "to": "salvar_contexto"},
            {"from": "salvar_contexto",  "to": "enviar_template"},
            {"from": "enviar_template",  "to": "marcar_notificado"},
        ],
    },
}

# Mapeamento flow → trigger_types para filtro
FLOW_TRIGGERS = {
    "webhook": ["webhook", "test"],
    "billing": ["billing_job"],
    "manutencao": ["manutencao_job"],
}


@router.get("/definition")
async def flow_definition(token: str = Query(None), flow: str = Query("webhook")):
    _check_token(token)
    if flow not in FLOW_DEFINITIONS:
        raise HTTPException(400, f"Flow invalido: {flow}. Validos: {list(FLOW_DEFINITIONS.keys())}")
    return FLOW_DEFINITIONS[flow]


@router.get("/executions")
async def list_executions(
    token: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    phone: str = Query(None),
    status: str = Query(None),
    trigger: str = Query(None),
    flow: str = Query(None),
):
    _check_token(token)
    from infra.supabase import get_supabase

    sb = get_supabase()
    if not sb:
        raise HTTPException(500, "Supabase indisponivel")

    q = (
        sb.table("ana_flow_executions")
        .select("id,phone,nome,trigger_type,status,started_at,finished_at,duration_ms,input_preview,output_preview,error_message")
        .order("started_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if phone:
        q = q.ilike("phone", f"%{phone[-8:]}%")
    if status:
        q = q.eq("status", status)
    if trigger:
        q = q.eq("trigger_type", trigger)
    elif flow and flow in FLOW_TRIGGERS:
        q = q.in_("trigger_type", FLOW_TRIGGERS[flow])

    result = q.execute()
    return {"executions": result.data, "count": len(result.data)}


@router.get("/executions/{execution_id}")
async def get_execution(execution_id: str, token: str = Query(None)):
    _check_token(token)
    from infra.supabase import get_supabase

    sb = get_supabase()
    if not sb:
        raise HTTPException(500, "Supabase indisponivel")

    exec_result = (
        sb.table("ana_flow_executions")
        .select("*")
        .eq("id", execution_id)
        .execute()
    )
    if not exec_result.data:
        raise HTTPException(404, "Execucao nao encontrada")

    nodes_result = (
        sb.table("ana_flow_nodes")
        .select("*")
        .eq("execution_id", execution_id)
        .order("node_order", desc=False)
        .execute()
    )

    return {
        "execution": exec_result.data[0],
        "nodes": nodes_result.data,
    }


@router.get("/stats")
async def flow_stats(token: str = Query(None), flow: str = Query(None)):
    _check_token(token)
    from infra.supabase import get_supabase

    sb = get_supabase()
    if not sb:
        raise HTTPException(500, "Supabase indisponivel")

    now = datetime.now(TZ_MT)
    since_24h = (now - timedelta(hours=24)).isoformat()

    q = (
        sb.table("ana_flow_executions")
        .select("status,duration_ms")
        .gte("started_at", since_24h)
    )
    if flow and flow in FLOW_TRIGGERS:
        q = q.in_("trigger_type", FLOW_TRIGGERS[flow])

    result = q.execute()

    data = result.data or []
    total = len(data)
    completed = sum(1 for d in data if d["status"] == "completed")
    errors = sum(1 for d in data if d["status"] == "error")
    durations = [d["duration_ms"] for d in data if d.get("duration_ms")]
    avg_duration = int(sum(durations) / len(durations)) if durations else 0

    return {
        "period": "24h",
        "total": total,
        "completed": completed,
        "errors": errors,
        "avg_duration_ms": avg_duration,
    }
