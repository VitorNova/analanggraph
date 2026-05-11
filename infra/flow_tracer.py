"""
Flow Tracer — instrumentacao do fluxo de atendimento.

Usa ContextVar para carregar execution_id pela cadeia async.
Escrita no Supabase eh fire-and-forget (nunca bloqueia o fluxo principal).
"""

import asyncio
import contextvars
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ContextVar carrega estado da execucao por toda a cadeia async
_execution_ctx: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "execution_ctx", default=None
)
_node_counter: contextvars.ContextVar[int] = contextvars.ContextVar(
    "node_counter", default=0
)

TZ_MT = timezone(timedelta(hours=-4))

# Chaves que contem dados grandes (base64) — excluidas do log
_EXCLUDE_KEYS = {"imagem_base64", "audio_base64", "documento_base64", "base64"}

MAX_DATA_LEN = 2000


def _truncate(value: Optional[str]) -> str:
    if not value:
        return ""
    return value[:MAX_DATA_LEN] if len(value) > MAX_DATA_LEN else value


def _clean_data(data) -> str:
    """Converte para string, remove campos base64, trunca."""
    if data is None:
        return ""
    if isinstance(data, str):
        return _truncate(data)
    if isinstance(data, dict):
        cleaned = {
            k: ("...[base64]..." if k in _EXCLUDE_KEYS else v)
            for k, v in data.items()
        }
        import json
        return _truncate(json.dumps(cleaned, ensure_ascii=False, default=str))
    if isinstance(data, (list, tuple)):
        import json
        return _truncate(json.dumps(data, ensure_ascii=False, default=str))
    return _truncate(str(data))


def start_execution(
    phone: str, input_preview: str = "", trigger: str = "webhook", nome: str = ""
) -> str:
    """Cria execucao e seta no ContextVar. Retorna execution_id."""
    exec_id = str(uuid.uuid4())
    ctx = {
        "id": exec_id,
        "phone": phone,
        "nome": nome,
        "trigger": trigger,
        "input_preview": _truncate(input_preview),
        "started_at": time.time(),
        "nodes": [],
    }
    _execution_ctx.set(ctx)
    _node_counter.set(0)
    logger.info(f"[FLOW:{phone[-4:]}] Execucao {exec_id[:8]} iniciada")
    return exec_id


@asynccontextmanager
async def trace_node(name: str, input_data=None):
    """Context manager async que registra execucao de um node.

    Uso:
        async with trace_node("pause_check", input_data=phone):
            # codigo real aqui
            result = await do_something()
            node["output_data"] = str(result)  # opcional
    """
    ctx = _execution_ctx.get(None)
    if ctx is None:
        # Sem execucao ativa — no-op
        yield {}
        return

    order = _node_counter.get(0)
    _node_counter.set(order + 1)

    node = {
        "node_name": name,
        "node_order": order,
        "started_at": time.time(),
        "input_data": _clean_data(input_data),
        "output_data": "",
        "status": "running",
        "error_message": "",
        "metadata": {},
    }

    try:
        yield node
        if node["status"] == "running":
            node["status"] = "success"
    except Exception as e:
        node["status"] = "error"
        node["error_message"] = str(e)[:500]
        raise
    finally:
        node["finished_at"] = time.time()
        node["duration_ms"] = int((node["finished_at"] - node["started_at"]) * 1000)
        node["output_data"] = _clean_data(node.get("output_data", ""))
        ctx["nodes"].append(node)


def trace_node_sync(name: str, input_data=None):
    """Versao sync para funcoes nao-async. Retorna dict do node.

    Uso:
        node = trace_node_sync("enviar_resposta", input_data=phone)
        try:
            resultado = funcao_sync()
            node["output_data"] = str(resultado)
            node["status"] = "success"
        except Exception as e:
            node["status"] = "error"
            node["error_message"] = str(e)[:500]
            raise
        finally:
            _finalize_node_sync(node)
    """
    ctx = _execution_ctx.get(None)
    if ctx is None:
        return {"_noop": True}

    order = _node_counter.get(0)
    _node_counter.set(order + 1)

    node = {
        "node_name": name,
        "node_order": order,
        "started_at": time.time(),
        "input_data": _clean_data(input_data),
        "output_data": "",
        "status": "running",
        "error_message": "",
        "metadata": {},
    }
    return node


def finalize_node_sync(node: dict):
    """Finaliza node sync e adiciona ao contexto."""
    if node.get("_noop"):
        return
    ctx = _execution_ctx.get(None)
    if ctx is None:
        return
    node["finished_at"] = time.time()
    node["duration_ms"] = int((node["finished_at"] - node["started_at"]) * 1000)
    node["output_data"] = _clean_data(node.get("output_data", ""))
    ctx["nodes"].append(node)


async def finish_execution(
    status: str = "completed", output_preview: str = "", error_msg: str = ""
):
    """Finaliza execucao e faz flush pro Supabase (fire-and-forget)."""
    ctx = _execution_ctx.get(None)
    if ctx is None:
        return

    ctx["status"] = status
    ctx["output_preview"] = _truncate(output_preview)
    ctx["error_message"] = error_msg[:500] if error_msg else ""
    ctx["finished_at"] = time.time()
    ctx["duration_ms"] = int((ctx["finished_at"] - ctx["started_at"]) * 1000)

    logger.info(
        f"[FLOW:{ctx['phone'][-4:]}] Execucao {ctx['id'][:8]} "
        f"finalizada ({status}, {ctx['duration_ms']}ms, {len(ctx['nodes'])} nodes)"
    )

    # Fire-and-forget
    asyncio.create_task(_flush_to_supabase(ctx))
    _execution_ctx.set(None)


async def _flush_to_supabase(ctx: dict):
    """Grava execucao + nodes no Supabase. Nunca levanta excecao."""
    try:
        from infra.supabase import get_supabase

        sb = get_supabase()
        if not sb:
            return

        now = datetime.now(TZ_MT).isoformat()

        # Inserir execucao
        exec_data = {
            "id": ctx["id"],
            "phone": ctx["phone"],
            "nome": ctx.get("nome", ""),
            "trigger_type": ctx.get("trigger", "webhook"),
            "status": ctx.get("status", "completed"),
            "started_at": datetime.fromtimestamp(
                ctx["started_at"], tz=TZ_MT
            ).isoformat(),
            "finished_at": datetime.fromtimestamp(
                ctx["finished_at"], tz=TZ_MT
            ).isoformat()
            if ctx.get("finished_at")
            else None,
            "duration_ms": ctx.get("duration_ms"),
            "input_preview": ctx.get("input_preview", ""),
            "output_preview": ctx.get("output_preview", ""),
            "error_message": ctx.get("error_message", ""),
            "metadata": {},
            "created_at": now,
        }
        sb.table("ana_flow_executions").insert(exec_data).execute()

        # Inserir nodes em batch
        if ctx["nodes"]:
            nodes_data = []
            for n in ctx["nodes"]:
                nodes_data.append(
                    {
                        "execution_id": ctx["id"],
                        "node_name": n["node_name"],
                        "node_order": n["node_order"],
                        "status": n.get("status", "success"),
                        "started_at": datetime.fromtimestamp(
                            n["started_at"], tz=TZ_MT
                        ).isoformat(),
                        "finished_at": datetime.fromtimestamp(
                            n["finished_at"], tz=TZ_MT
                        ).isoformat()
                        if n.get("finished_at")
                        else None,
                        "duration_ms": n.get("duration_ms"),
                        "input_data": n.get("input_data", ""),
                        "output_data": n.get("output_data", ""),
                        "error_message": n.get("error_message", ""),
                        "metadata": n.get("metadata", {}),
                        "created_at": now,
                    }
                )
            sb.table("ana_flow_nodes").insert(nodes_data).execute()

        logger.debug(
            f"[FLOW] Flush OK: exec {ctx['id'][:8]}, {len(ctx['nodes'])} nodes"
        )

    except Exception as e:
        logger.warning(f"[FLOW] Flush falhou (nao-bloqueante): {e}")


def get_current_execution_id() -> Optional[str]:
    """Retorna execution_id ativo ou None."""
    ctx = _execution_ctx.get(None)
    return ctx["id"] if ctx else None
