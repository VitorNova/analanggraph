"""Persistência — salvar/buscar histórico no Supabase.

Funções:
- upsert_lead: criar ou atualizar lead
- salvar_mensagem: salvar no conversation_history
- buscar_historico: buscar últimas N msgs como LangChain messages
- salvar_mensagens_agente: salvar respostas do agente
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from langchain_core.messages import AIMessage, HumanMessage, BaseMessage, ToolMessage

logger = logging.getLogger(__name__)

from core.constants import TABLE_LEADS
from infra.supabase import get_supabase


def upsert_lead(telefone: str, nome: str = None) -> Optional[str]:
    """Cria ou atualiza lead, retorna lead_id."""
    supabase = get_supabase()
    if not supabase:
        return None

    now = datetime.now(timezone.utc).isoformat()

    try:
        existing = supabase.table(TABLE_LEADS) \
            .select("id").eq("telefone", telefone).execute()

        if existing.data:
            lead_id = existing.data[0]["id"]
            update = {"last_interaction_at": now, "updated_at": now}
            if nome:
                update["nome"] = nome
            supabase.table(TABLE_LEADS).update(update).eq("id", lead_id).execute()
            return lead_id
        else:
            result = supabase.table(TABLE_LEADS).insert({
                "telefone": telefone,
                "nome": nome or f"Lead {telefone}",
                "current_state": "ai",
                "responsavel": "AI",
                "last_interaction_at": now,
                "created_at": now,
                "updated_at": now,
            }).execute()
            return result.data[0]["id"] if result.data else None
    except Exception as e:
        logger.error(f"[PERSISTENCIA] Erro upsert_lead: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(telefone, "upsert_lead_erro", str(e)[:300])
        return None


def salvar_mensagem(telefone: str, content: str, direction: str, lead_id: str = None):
    """Salva mensagem no conversation_history da tabela ana_leads."""
    supabase = get_supabase()
    if not supabase:
        return

    role = "user" if direction == "incoming" else "model"
    now = datetime.now(timezone.utc).isoformat()

    try:
        existing = supabase.table(TABLE_LEADS) \
            .select("id, conversation_history") \
            .eq("telefone", telefone).limit(1).execute()

        if not existing.data:
            return  # Lead não existe, upsert_lead deveria ter criado

        new_msg = {"role": role, "content": content, "timestamp": now}
        history = existing.data[0].get("conversation_history") or {"messages": []}
        history["messages"].append(new_msg)

        supabase.table(TABLE_LEADS) \
            .update({"conversation_history": history, "updated_at": now}) \
            .eq("id", existing.data[0]["id"]).execute()
    except Exception as e:
        logger.error(f"[PERSISTENCIA] Erro salvar_mensagem: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(telefone, "salvar_msg_erro", str(e)[:300])


def buscar_historico(telefone: str, limite: int = 20) -> List[BaseMessage]:
    """Busca últimas N mensagens como objetos LangChain.

    Inclui validação de sequência: remove ToolMessage órfãs e
    blocos incompletos de tool_calls que o Gemini rejeitaria.
    """
    supabase = get_supabase()
    if not supabase:
        return []

    try:
        result = supabase.table(TABLE_LEADS) \
            .select("conversation_history") \
            .eq("telefone", telefone).limit(1).execute()

        if not result.data:
            return []

        history = result.data[0].get("conversation_history") or {"messages": []}
        messages = history.get("messages", [])[-limite:]

        lang_msgs = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "user":
                lang_msgs.append(HumanMessage(content=content))
            elif role == "model":
                tool_calls = m.get("tool_calls")
                if tool_calls:
                    lang_msgs.append(AIMessage(content=content, tool_calls=tool_calls))
                else:
                    # Sanitizar tool-as-text: Gemini 2.0 Flash às vezes escreve
                    # nome de tool como texto no content em vez de usar function calling.
                    # Se o histórico tem isso, limpar para não contaminar futuras respostas.
                    if "transferir_departamento(" in content or "consultar_cliente(" in content or "registrar_compromisso(" in content:
                        content = ""
                    lang_msgs.append(AIMessage(content=content))
            elif role == "tool":
                lang_msgs.append(ToolMessage(
                    content=content,
                    name=m.get("tool_name", ""),
                    tool_call_id=m.get("tool_call_id", "unknown"),
                ))

        # Validar sequência para Gemini:
        # - AIMessage(tool_calls) deve ser seguida de ToolMessage(s) correspondentes
        # - ToolMessage deve ter AIMessage(tool_calls) antes
        # Se o corte de histórico quebrou a sequência, remover mensagens órfãs.
        validated: List[BaseMessage] = []
        pending_tool_ids: set = set()

        for msg in lang_msgs:
            if isinstance(msg, AIMessage) and msg.tool_calls:
                if pending_tool_ids:
                    # AIMessage anterior com tool_calls não teve todas as ToolMessages
                    while validated and (
                        isinstance(validated[-1], ToolMessage)
                        or (isinstance(validated[-1], AIMessage) and validated[-1].tool_calls)
                    ):
                        removed = validated.pop()
                        logger.warning(f"[PERSISTENCIA:{telefone}] Removida mensagem órfã: {type(removed).__name__}")
                    pending_tool_ids.clear()
                pending_tool_ids = {tc["id"] for tc in msg.tool_calls if "id" in tc}
                validated.append(msg)
            elif isinstance(msg, ToolMessage):
                if pending_tool_ids:
                    validated.append(msg)
                    tool_id = getattr(msg, "tool_call_id", "")
                    pending_tool_ids.discard(tool_id)
                else:
                    logger.warning(f"[PERSISTENCIA:{telefone}] Removida ToolMessage órfã (sem AIMessage antes)")
            else:
                if pending_tool_ids:
                    # Mensagem chegou antes de completar ToolMessages — remover bloco incompleto
                    while validated and (
                        isinstance(validated[-1], ToolMessage)
                        or (isinstance(validated[-1], AIMessage) and validated[-1].tool_calls)
                    ):
                        removed = validated.pop()
                        logger.warning(f"[PERSISTENCIA:{telefone}] Removida mensagem órfã: {type(removed).__name__}")
                    pending_tool_ids.clear()
                validated.append(msg)

        # Se terminou com tool_calls pendentes, remover bloco incompleto
        if pending_tool_ids:
            while validated and (
                isinstance(validated[-1], ToolMessage)
                or (isinstance(validated[-1], AIMessage) and validated[-1].tool_calls)
            ):
                removed = validated.pop()
                logger.warning(f"[PERSISTENCIA:{telefone}] Removida mensagem órfã no final: {type(removed).__name__}")

        return validated
    except Exception as e:
        logger.error(f"[PERSISTENCIA] Erro buscar_historico: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(telefone, "historico_busca_erro", str(e)[:300])
        return []


def salvar_mensagens_agente(telefone: str, mensagens: List[BaseMessage], usage: dict = None):
    """Salva todas as mensagens do agente (AIMessage com tool_calls + ToolMessage).

    Args:
        telefone: Telefone do lead.
        mensagens: Lista de mensagens do agente (AIMessage, ToolMessage).
        usage: Token usage do Gemini (opcional). Dict com 'input', 'output', 'total'.
    """
    supabase = get_supabase()
    if not supabase:
        return

    try:
        now = datetime.now(timezone.utc).isoformat()

        result = supabase.table(TABLE_LEADS) \
            .select("id, conversation_history") \
            .eq("telefone", telefone).limit(1).execute()

        if not result.data:
            return

        lead = result.data[0]
        lead_id = lead["id"]
        history = lead.get("conversation_history") or {"messages": []}

        # Encontrar última AIMessage com texto (para adicionar token_count)
        last_text_ai_idx = None
        for i, msg in enumerate(mensagens):
            if isinstance(msg, AIMessage) and not msg.tool_calls:
                last_text_ai_idx = i

        for i, msg in enumerate(mensagens):
            if isinstance(msg, AIMessage):
                raw = msg.content
                if isinstance(raw, list):
                    text = " ".join(
                        block["text"] for block in raw
                        if isinstance(block, dict) and block.get("text")
                    )
                else:
                    text = raw or ""
                entry = {
                    "role": "model",
                    "content": text,
                    "timestamp": now,
                }
                if msg.tool_calls:
                    entry["tool_calls"] = msg.tool_calls
                if usage and usage.get("total") and i == last_text_ai_idx:
                    entry["token_count"] = usage["total"]
                history["messages"].append(entry)

            elif isinstance(msg, ToolMessage):
                history["messages"].append({
                    "role": "tool",
                    "content": msg.content,
                    "tool_name": msg.name,
                    "tool_call_id": msg.tool_call_id,
                    "timestamp": now,
                })

        supabase.table(TABLE_LEADS).update({
            "conversation_history": history,
            "updated_at": now,
            "last_interaction_at": now,
        }).eq("id", lead_id).execute()

        logger.info(f"[PERSISTENCIA:{telefone}] Salvas {len(mensagens)} mensagens do agente")

    except Exception as e:
        logger.error(f"[PERSISTENCIA:{telefone}] Erro salvar_mensagens_agente: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(telefone, "historico_erro", f"salvar_mensagens_agente: {e}"[:300])


