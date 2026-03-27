"""
Template: Grafo LangGraph ReAct para agente WhatsApp.

Baseado em: /var/www/agente-langgraph/core/agente/fluxo.py (produção)

Fluxo: Webhook → Buffer (9s) → processar_mensagens() → graph.ainvoke() → WhatsApp

Uso:
    1. Copie e ajuste TOOLS e SYSTEM_PROMPT
    2. O buffer chama processar_mensagens(phone, messages, context) como callback
"""

import asyncio
import json as _json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import Annotated, TypedDict

logger = logging.getLogger(__name__)

from core.tools import TOOLS
from core.prompts import SYSTEM_PROMPT

MAX_TENTATIVAS = 2
TIMEZONE_OFFSET = -4  # UTC-4 (Mato Grosso)
FALLBACK_MSG = "Desculpe, ocorreu um erro interno. Por favor, tente novamente em alguns instantes."
ADMIN_PHONE = os.environ.get("ADMIN_PHONE")


# =============================================================================
# STATE
# =============================================================================

class State(TypedDict):
    """Estado do agente LangGraph."""
    messages: Annotated[list, add_messages]
    phone: str


# =============================================================================
# MODEL
# =============================================================================

def get_model():
    """Instancia Gemini com tools vinculadas."""
    import os
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.0-flash",
        google_api_key=os.environ.get("GOOGLE_API_KEY"),
        temperature=0.3,
        max_output_tokens=4096,
        transport="rest",
    )
    return llm.bind_tools(TOOLS) if TOOLS else llm


# =============================================================================
# GRAPH NODES
# =============================================================================

async def call_model(state: State) -> dict:
    """Invoca LLM com system prompt + histórico."""
    # Injetar data/hora atual no prompt
    now = datetime.now(timezone(timedelta(hours=TIMEZONE_OFFSET)))
    system_time = now.strftime("%d/%m/%Y %H:%M") + f" (timezone UTC{TIMEZONE_OFFSET:+d})"

    prompt = SYSTEM_PROMPT.replace("{system_time}", system_time)

    # Contexto extra (billing/manutenção) é injetado por processar_mensagens()
    # via _context_extra, evitando query ao Supabase em cada iteração do loop ReAct
    extra = _context_extra.get(state.get("phone", ""), "")
    if extra:
        prompt += "\n\n" + extra

    messages = [SystemMessage(content=prompt)] + state["messages"]
    response = await get_model().ainvoke(messages)

    return {"messages": [response]}


# Cache de contexto por phone (preenchido em processar_mensagens, lido em call_model)
_context_extra: dict = {}


def route_model_output(state: State) -> Literal["tools", "__end__"]:
    """Se LLM chamou tool → 'tools', senão → END."""
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


async def call_tools(state: State) -> dict:
    """Executa tools chamadas pelo LLM."""
    tool_node = ToolNode(TOOLS)
    return await tool_node.ainvoke(state)


# =============================================================================
# GRAPH
# =============================================================================

def build_graph():
    """Constrói e compila o grafo ReAct."""
    builder = StateGraph(State)

    builder.add_node("call_model", call_model)
    builder.add_node("tools", call_tools)

    builder.set_entry_point("call_model")
    builder.add_conditional_edges("call_model", route_model_output)
    builder.add_edge("tools", "call_model")

    return builder.compile()


graph = build_graph()


# =============================================================================
# FALLBACK E NOTIFICAÇÃO DE ERRO
# =============================================================================

def _notificar_erro(phone: str, erro: Exception):
    """Log estruturado + WhatsApp para admin."""
    from infra.persistencia import enviar_resposta

    erro_info = {
        "event": "graph_invoke_failed",
        "phone": phone,
        "error_type": type(erro).__name__,
        "error_msg": str(erro)[:500],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    logger.error(f"[GRAFO:{phone}] {_json.dumps(erro_info)}")

    if ADMIN_PHONE:
        try:
            enviar_resposta(
                ADMIN_PHONE,
                f"[ERRO IA] Lead {phone}: {type(erro).__name__}: {str(erro)[:200]}",
            )
        except Exception:
            logger.exception(f"[GRAFO:{phone}] Falha ao notificar admin")


# =============================================================================
# ENTRY POINT (callback do buffer)
# =============================================================================

async def processar_mensagens(phone: str, messages: list, context: dict = None):
    """
    Processa mensagens acumuladas no buffer.

    Chamado pelo MessageBuffer após delay de 9s.

    Args:
        phone: Telefone do lead
        messages: Lista de msgs acumuladas no buffer
        context: Contexto opcional (nome, lead_id, mídia)
    """
    from infra.redis import get_redis_service
    from infra.persistencia import buscar_historico, salvar_mensagem, salvar_mensagens_agente, enviar_resposta

    redis = await get_redis_service()

    # 1. Verificar pausa
    if await redis.is_paused(phone):
        logger.info(f"[GRAFO:{phone}] IA pausada - ignorando")
        return

    # 2. Combinar mensagens do buffer
    textos = [m.get("texto", "") for m in messages if m.get("texto")]
    texto = "\n".join(textos)

    # Extrair mídia (última imagem/áudio/documento do buffer)
    imagem_base64 = None
    imagem_mimetype = "image/jpeg"
    audio_base64 = None
    audio_mimetype = "audio/ogg"
    documento_base64 = None
    documento_mimetype = "application/pdf"
    documento_nome = ""

    for msg in messages:
        if msg.get("imagem_base64"):
            imagem_base64 = msg["imagem_base64"]
            imagem_mimetype = msg.get("imagem_mimetype", "image/jpeg")
        if msg.get("audio_base64"):
            audio_base64 = msg["audio_base64"]
            audio_mimetype = msg.get("audio_mimetype", "audio/ogg")
        if msg.get("documento_base64"):
            documento_base64 = msg["documento_base64"]
            documento_mimetype = msg.get("documento_mimetype", "application/pdf")
            documento_nome = msg.get("documento_nome", "")

    has_media = imagem_base64 or audio_base64 or documento_base64

    if not texto and not has_media:
        return

    # 3. Buscar histórico
    historico = buscar_historico(phone, limite=20)

    # 4. Salvar mensagem do usuário (texto, sem base64)
    if texto:
        salvar_mensagem(phone, texto, "incoming")

    # 5. Detectar contexto (billing/manutenção) — 1x por mensagem, não por iteração
    try:
        from core.context_detector import detect_context, build_context_prompt
        from infra.supabase import get_supabase

        supabase = get_supabase()
        if supabase:
            ctx_result = supabase.table("ana_leads").select(
                "conversation_history"
            ).eq("telefone", phone).limit(1).execute()

            if ctx_result.data:
                history_data = ctx_result.data[0].get("conversation_history")
                context_type, reference_id = detect_context(history_data)
                if context_type:
                    _context_extra[phone] = build_context_prompt(context_type, reference_id)
                    logger.info(f"[GRAFO:{phone}] Contexto injetado: {context_type}")
    except Exception as e:
        logger.warning(f"[GRAFO:{phone}] Erro ao detectar contexto: {e}")

    # 6. Construir mensagens LangChain
    if imagem_base64:
        # HumanMessage multimodal (imagem)
        current_message = HumanMessage(content=[
            {"type": "text", "text": texto or "[Imagem enviada]"},
            {"type": "image_url", "image_url": f"data:{imagem_mimetype};base64,{imagem_base64}"},
        ])
        lang_messages = historico + [current_message]
    elif audio_base64:
        # HumanMessage multimodal (áudio)
        current_message = HumanMessage(content=[
            {"type": "text", "text": texto or "[Áudio enviado]"},
            {"type": "media", "data": audio_base64, "mime_type": audio_mimetype},
        ])
        lang_messages = historico + [current_message]
    elif documento_base64:
        # HumanMessage multimodal (documento/PDF)
        current_message = HumanMessage(content=[
            {"type": "text", "text": texto or f"[Documento: {documento_nome}]"},
            {"type": "media", "data": documento_base64, "mime_type": documento_mimetype},
        ])
        lang_messages = historico + [current_message]
    else:
        lang_messages = historico + [HumanMessage(content=texto)]

    # 7. Invocar grafo com retry (phone injetado via InjectedState nas tools)
    result = None
    last_error = None
    for tentativa in range(MAX_TENTATIVAS):
        try:
            result = await graph.ainvoke(
                {"messages": lang_messages, "phone": phone},
            )
            break
        except Exception as e:
            last_error = e
            logger.error(f"[GRAFO:{phone}] Erro tentativa {tentativa+1}: {e}")
            if tentativa < MAX_TENTATIVAS - 1:
                await asyncio.sleep(2)

    if result is None:
        _context_extra.pop(phone, None)
        enviar_resposta(phone, FALLBACK_MSG)
        if last_error:
            _notificar_erro(phone, last_error)
        return

    # 7. Extrair mensagens novas do agente (AIMessage + ToolMessage)
    qtd_enviadas = len(lang_messages)
    novas_mensagens = result["messages"][qtd_enviadas:]
    mensagens_agente = [
        m for m in novas_mensagens
        if isinstance(m, (AIMessage, ToolMessage))
    ]

    # Extrair usage da última AIMessage
    usage = {}
    for m in reversed(mensagens_agente):
        if isinstance(m, AIMessage) and hasattr(m, "usage_metadata") and m.usage_metadata:
            um = m.usage_metadata
            usage = {
                "input": um.get("input_tokens") or um.get("prompt_tokens", 0),
                "output": um.get("output_tokens") or um.get("completion_tokens", 0),
                "total": um.get("total_tokens", 0),
            }
            break

    # Salvar todas as mensagens do agente (incluindo tool_calls)
    if mensagens_agente:
        salvar_mensagens_agente(phone, mensagens_agente, usage=usage or None)

    # 8. Extrair resposta final e enviar
    resposta = None
    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            content = msg.content
            # Gemini 3.x pode retornar lista
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            if content.strip():
                resposta = content.strip()
                break

    # Limpar cache de contexto
    _context_extra.pop(phone, None)

    if resposta:
        enviar_resposta(phone, resposta, agent_name="Ana")
    else:
        enviar_resposta(phone, FALLBACK_MSG)
