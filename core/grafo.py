"""
Template: Grafo LangGraph ReAct para agente WhatsApp.

Baseado em: /var/www/agente-langgraph/core/agente/fluxo.py (produção)

Fluxo: Webhook → Buffer (9s) → processar_mensagens() → graph.ainvoke() → WhatsApp

Uso:
    1. Copie e ajuste TOOLS e SYSTEM_PROMPT
    2. O buffer chama processar_mensagens(phone, messages, context) como callback
"""

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

TIMEZONE_OFFSET = -4  # UTC-4 (Mato Grosso)

# Filas onde a IA responde (importado de constants)
from core.constants import IA_QUEUES, TABLE_LEADS, QUEUE_IA, USER_IA
from core.constants import FALLBACK_MSG

# ---- Transcrição de áudio via Gemini Flash ----
def _transcrever_audio(audio_base64: str, mime_type: str = "audio/ogg"):
    """Transcreve áudio usando Gemini Flash. Retorna texto ou None."""
    try:
        import google.generativeai as genai
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content([
            {"mime_type": mime_type, "data": __import__("base64").b64decode(audio_base64)},
            "Transcreva este áudio literalmente em português. Retorne APENAS o texto falado, sem comentários.",
        ])
        texto = (response.text or "").strip()
        if texto:
            logger.info(f"[TRANSCRIÇÃO] OK: {texto[:80]}")
            return texto
    except Exception as e:
        logger.warning(f"[TRANSCRIÇÃO] Falha: {e}")
    return None
MAX_TOOL_ROUNDS = 5
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

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")


def _build_model():
    """Instancia Gemini com tools vinculadas (chamado 1x)."""
    from google.ai.generativelanguage_v1beta.types import HarmCategory, SafetySetting

    logger.info(f"[MODEL] Inicializando modelo: {GEMINI_MODEL}")
    llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=os.environ.get("GOOGLE_API_KEY"),
        temperature=0.0,
        max_output_tokens=4096,
        transport="rest",
        safety_settings={
            HarmCategory.HARM_CATEGORY_HARASSMENT: SafetySetting.HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: SafetySetting.HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: SafetySetting.HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: SafetySetting.HarmBlockThreshold.BLOCK_NONE,
        },
    )
    return llm.bind_tools(TOOLS, tool_choice="auto") if TOOLS else llm


_model = None


def get_model():
    """Retorna singleton do modelo (lazy init)."""
    global _model
    if _model is None:
        _model = _build_model()
    return _model


# =============================================================================
# GRAPH NODES
# =============================================================================

async def call_model(state: State) -> dict:
    """Invoca LLM com system prompt + histórico.

    Inclui guardrail antierro: se a resposta afirma ter executado uma tool
    sem realmente tê-la chamado, faz retry (max 1x) e fallback.
    A resposta errada NUNCA entra no State.
    """
    # Injetar data/hora atual no prompt
    now = datetime.now(timezone(timedelta(hours=TIMEZONE_OFFSET)))
    system_time = now.strftime("%d/%m/%Y %H:%M") + f" (timezone UTC{TIMEZONE_OFFSET:+d})"

    prompt = SYSTEM_PROMPT.replace("{system_time}", system_time)

    # Contexto extra (billing/manutenção) é injetado por processar_mensagens()
    # via _context_extra, evitando query ao Supabase em cada iteração do loop ReAct
    ctx_data = _context_extra.get(state.get("phone", ""))
    if ctx_data:
        prompt += "\n\n" + ctx_data["prompt"]

    messages = [SystemMessage(content=prompt)] + state["messages"]
    response = await get_model().ainvoke(messages)

    # =========================================================================
    # GUARDRAIL ANTIERRO — resposta errada nunca entra no State
    # =========================================================================
    if not response.tool_calls:
        # Normalizar content para string (Gemini 2.5 pode retornar list[dict])
        content_str = ""
        if response.content:
            if isinstance(response.content, list):
                content_str = " ".join(
                    p.get("text", "") for p in response.content if isinstance(p, dict)
                )
            else:
                content_str = str(response.content)

        if content_str.strip():
            # Coletar tools já chamadas nesta sessão (ToolMessages no state)
            _tool_names_in_session = {
                m.name for m in state["messages"]
                if isinstance(m, ToolMessage) and hasattr(m, "name")
            }

            from core.hallucination import checar_resposta_pre_envio
            violations = checar_resposta_pre_envio(content_str, _tool_names_in_session)

            if violations:
                phone = state.get("phone", "?")
                tool_violada = violations[0][0]
                logger.warning(
                    f"[ANTIERRO:{phone}] Hallucination detectada PRÉ-envio: "
                    f"{tool_violada} não chamada. Tentando retry..."
                )

                # Log do incidente
                try:
                    from infra.incidentes import registrar_incidente
                    registrar_incidente(
                        phone, "hallucination",
                        f"Antierro PRÉ-envio: {tool_violada} não chamada",
                        {"resposta_original": content_str[:300], "acao": "retry"},
                    )
                except Exception:
                    pass

                # CAMADA 2: Retry — devolver ao LLM com instrução de correção
                messages_retry = list(messages) + [
                    response,
                    HumanMessage(content=(
                        "[SISTEMA — NÃO RESPONDA A ESTA MENSAGEM COMO SE FOSSE DO CLIENTE]\n\n"
                        f"⚠️ CORREÇÃO OBRIGATÓRIA: sua resposta acima afirmou que você fez "
                        f"{tool_violada}, mas você NÃO chamou a ferramenta. Isso é PROIBIDO.\n\n"
                        f"VOCÊ DEVE fazer UMA dessas coisas:\n"
                        f"1. Chamar {tool_violada}() com os argumentos corretos (PREFERÍVEL)\n"
                        f"2. Se não conseguir, reformule sem afirmar que já fez a ação.\n\n"
                        "Responda ao cliente normalmente — reformule sua resposta anterior."
                    )),
                ]
                response = await get_model().ainvoke(messages_retry)

                # Se retry retornou tool_calls → ótimo, grafo executa normalmente
                if response.tool_calls:
                    logger.info(f"[ANTIERRO:{phone}] Retry corrigiu: {tool_violada} chamada via tool_call")
                    return {"messages": [response]}

                # CAMADA 3: Contingência — se era transferência, inferir e executar
                if tool_violada == "transferir_departamento":
                    from core.hallucination import inferir_destino_do_texto
                    retry_content = ""
                    if response.content:
                        if isinstance(response.content, list):
                            retry_content = " ".join(
                                p.get("text", "") for p in response.content if isinstance(p, dict)
                            )
                        else:
                            retry_content = str(response.content)
                    destino = inferir_destino_do_texto(retry_content or content_str)
                    if destino:
                        logger.info(f"[ANTIERRO:{phone}] Contingência: forçando transferência → {destino}")
                        # Criar tool_call sintético para o grafo executar
                        response = AIMessage(
                            content="",
                            tool_calls=[{
                                "name": "transferir_departamento",
                                "args": {"destino": destino},
                                "id": "antierro_contingencia",
                            }],
                        )
                        return {"messages": [response]}

                # CAMADA 4: Fallback — substituir por mensagem segura
                logger.warning(f"[ANTIERRO:{phone}] Retry falhou, aplicando fallback")
                response = AIMessage(content=FALLBACK_MSG)

            # =================================================================
            # CAMADA 1b: contexto exige tool mas LLM não chamou (omissão)
            # Ex: manutenção preventiva → LLM disse "equipe vai entrar em contato"
            # mas não chamou transferir_departamento. Diferente da camada 1 que
            # detecta hallucination de texto, esta detecta FALTA de ação.
            # =================================================================
            if not violations:
                ctx_data_guard = _context_extra.get(state.get("phone", ""))
                if ctx_data_guard:
                    from core.hallucination import checar_contexto_sem_tool
                    ctx_violation = checar_contexto_sem_tool(
                        ctx_data_guard["type"], content_str, _tool_names_in_session,
                    )
                    if ctx_violation:
                        tool_esperada, destino = ctx_violation
                        phone = state.get("phone", "?")
                        logger.warning(
                            f"[ANTIERRO:{phone}] Contexto {ctx_data_guard['type']}: "
                            f"{tool_esperada} não chamada. Retry..."
                        )

                        try:
                            from infra.incidentes import registrar_incidente
                            registrar_incidente(
                                phone, "contexto_sem_tool",
                                f"Contexto {ctx_data_guard['type']}: {tool_esperada} não chamada",
                                {"resposta_original": content_str[:300], "acao": "retry_contexto"},
                            )
                        except Exception:
                            pass

                        # CAMADA 2b: Retry — devolver ao LLM com instrução de contexto
                        messages_retry = list(messages) + [
                            response,
                            HumanMessage(content=(
                                "[SISTEMA — NÃO RESPONDA A ESTA MENSAGEM COMO SE FOSSE DO CLIENTE]\n\n"
                                "⚠️ CORREÇÃO OBRIGATÓRIA: você está em contexto de MANUTENÇÃO PREVENTIVA. "
                                "O cliente respondeu ao aviso de manutenção. "
                                "Você DEVE chamar transferir_departamento(destino=\"atendimento\") "
                                "para que a Nathália (fila 453, user 815) agende a visita.\n\n"
                                "Responda ao cliente E chame a ferramenta transferir_departamento."
                            )),
                        ]
                        response = await get_model().ainvoke(messages_retry)

                        if response.tool_calls:
                            logger.info(f"[ANTIERRO:{phone}] Retry contexto corrigiu: tool chamada")
                            return {"messages": [response]}

                        # CAMADA 3b: Forçar tool_call sintético → Nathália (453/815)
                        logger.warning(
                            f"[ANTIERRO:{phone}] Retry contexto falhou → "
                            f"forçando transferência → {destino}"
                        )
                        response = AIMessage(
                            content="A equipe técnica já vai entrar em contato pra agendar a manutenção!",
                            tool_calls=[{
                                "name": "transferir_departamento",
                                "args": {"destino": destino},
                                "id": "antierro_contexto_manutencao",
                            }],
                        )
                        return {"messages": [response]}

    return {"messages": [response]}


# Cache de contexto por phone (preenchido em processar_mensagens, lido em call_model)
_context_extra: dict = {}


def route_model_output(state: State) -> Literal["tools", "__end__"]:
    """Se LLM chamou tool → 'tools', senão → END. Limita rounds para evitar loops."""
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        # Se transferir_departamento já foi chamada com sucesso nesta invocação, encerrar
        # Evita loop onde Gemini chama transferir 3-5x seguidas
        for m in reversed(state["messages"]):
            if isinstance(m, HumanMessage):
                break
            if isinstance(m, ToolMessage) and hasattr(m, "name") and m.name == "transferir_departamento":
                content = str(m.content) if m.content else ""
                if "Transferido" in content or "sucesso" in content.lower():
                    logger.info("[GRAFO] transferir_departamento já executada — encerrando")
                    return END

        # Contar apenas tool rounds DESTA invocação (após último HumanMessage)
        # Ignora histórico de conversas anteriores
        rounds = 0
        for m in reversed(state["messages"]):
            if isinstance(m, HumanMessage):
                break
            if isinstance(m, AIMessage) and m.tool_calls:
                rounds += 1
        if rounds >= MAX_TOOL_ROUNDS:
            logger.warning(f"[GRAFO] Limite de {MAX_TOOL_ROUNDS} tool rounds atingido — encerrando")
            return END
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
    """Log estruturado + notificação para admin via Leadbox."""
    from infra.leadbox_client import enviar_resposta_leadbox

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
            enviar_resposta_leadbox(
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
    from infra.nodes_supabase import buscar_historico, salvar_mensagem, salvar_mensagens_agente
    from infra.event_logger import log_event

    from infra.flow_tracer import start_execution, trace_node, finish_execution

    redis = await get_redis_service()

    # -- Flow tracer: iniciar execução --
    import json as _json
    texto_preview = " ".join(m.get("texto", "")[:50] for m in messages if m.get("texto"))
    nome_ctx = context.get("nome", "") if context else ""
    start_execution(phone, input_preview=texto_preview, nome=nome_ctx)

    # 0. Node buffer_9s — mensagens acumuladas
    async with trace_node("buffer_9s", input_data=_json.dumps({
        "telefone": phone,
        "mensagens_no_buffer": len(messages),
        "nome": nome_ctx,
    }, ensure_ascii=False)) as nd_buf:
        buf_msgs = []
        for m in messages:
            entry = {}
            if m.get("texto"):
                entry["texto"] = m["texto"][:200]
            if m.get("imagem_base64"):
                entry["midia"] = "imagem"
            if m.get("audio_base64"):
                entry["midia"] = "audio"
            if m.get("documento_base64"):
                entry["midia"] = f"documento ({m.get('documento_nome', '')})"
            buf_msgs.append(entry)
        nd_buf["output_data"] = _json.dumps({"mensagens": buf_msgs}, ensure_ascii=False)[:1500]

    # 1. Verificar pausa (Redis)
    async with trace_node("pause_check", input_data=_json.dumps({"telefone": phone}, ensure_ascii=False)) as nd:
        paused = await redis.is_paused(phone)
        nd["output_data"] = _json.dumps({"pausado": paused}, ensure_ascii=False)
    if paused:
        logger.info(f"[GRAFO:{phone}] IA pausada - ignorando")
        await finish_execution("completed", "paused")
        return

    # 1b. Fail-safe: verificar fila no Supabase (DB pode estar mais atualizado que Redis)
    current_queue = QUEUE_IA  # default para lead novo ou falha na query
    async with trace_node("queue_check", input_data=_json.dumps({"telefone": phone}, ensure_ascii=False)) as nd_q:
        try:
            from infra.supabase import get_supabase
            _sb = get_supabase()
            if _sb:
                _lead = _sb.table(TABLE_LEADS).select(
                    "current_queue_id, current_state"
                ).eq("telefone", phone).limit(1).execute()
                if _lead.data:
                    _queue = _lead.data[0].get("current_queue_id")
                    _state = _lead.data[0].get("current_state")
                    if _queue is not None:
                        current_queue = int(_queue)
                    nd_q["output_data"] = _json.dumps({"fila": _queue, "estado": _state}, ensure_ascii=False)
                    if _state == "human":
                        logger.info(f"[GRAFO:{phone}] Fail-safe: state=human - ignorando")
                        await redis.pause_set(phone)
                        await finish_execution("completed", "queue_human")
                        return
                    if _queue is not None and int(_queue) not in IA_QUEUES:
                        logger.info(f"[GRAFO:{phone}] Fail-safe: fila {_queue} (humana) - ignorando")
                        await redis.pause_set(phone)
                        await finish_execution("completed", "queue_not_ia")
                        return
                else:
                    nd_q["output_data"] = "lead_novo"
        except Exception as e:
            logger.warning(f"[GRAFO:{phone}] Fail-safe check falhou: {e}")
            from infra.incidentes import registrar_incidente
            registrar_incidente(phone, "consulta_falhou", f"Fail-safe Supabase: {e}"[:300])
            nd_q["output_data"] = f"erro: {e}"

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
        await finish_execution("completed", "empty_input")
        return

    log_event("msg_received", phone, text=texto[:100] if texto else "[media]")

    # 3. Buscar histórico
    async with trace_node("historico", input_data=_json.dumps({"telefone": phone, "limite": 20}, ensure_ascii=False)) as nd_h:
        historico = buscar_historico(phone, limite=20)
        # Resumo das últimas msgs do histórico
        hist_resumo = []
        for h in historico[-5:]:
            content = h.content if isinstance(h.content, str) else str(h.content)
            hist_resumo.append({"tipo": h.__class__.__name__, "conteudo": content[:150]})
        nd_h["output_data"] = _json.dumps({"total": len(historico), "ultimas_5": hist_resumo}, ensure_ascii=False)[:1500]

    # 4. Transcrever áudio (se houver) antes de salvar
    audio_transcricao = None
    if audio_base64:
        async with trace_node("transcricao", input_data=f"mime={audio_mimetype}") as nd_t:
            audio_transcricao = _transcrever_audio(audio_base64, audio_mimetype)
            nd_t["output_data"] = audio_transcricao[:200] if audio_transcricao else "falhou"

    # 5. Salvar mensagem do usuário (texto ou marcador de mídia)
    if texto:
        salvar_mensagem(phone, texto, "incoming")
    elif has_media:
        if audio_base64 and audio_transcricao:
            media_label = f'[Áudio transcrito: "{audio_transcricao}"]'
        elif audio_base64:
            media_label = "[Áudio enviado]"
        elif imagem_base64:
            media_label = "[Imagem enviada]"
        else:
            media_label = f"[Documento: {documento_nome}]"
        salvar_mensagem(phone, media_label, "incoming")

    # 5. Detectar contexto (billing/manutenção) — 1x por mensagem, não por iteração
    async with trace_node("context_detection", input_data=_json.dumps({"telefone": phone}, ensure_ascii=False)) as nd_ctx:
        try:
            from core.context_detector import detect_context, build_context_prompt
            from infra.supabase import get_supabase

            supabase = get_supabase()
            if supabase:
                ctx_result = supabase.table(TABLE_LEADS).select(
                    "conversation_history"
                ).eq("telefone", phone).limit(1).execute()

                if ctx_result.data:
                    history_data = ctx_result.data[0].get("conversation_history")
                    context_type, reference_id = detect_context(history_data)
                    if context_type:
                        _context_extra[phone] = {
                            "type": context_type,
                            "prompt": build_context_prompt(context_type, reference_id),
                        }
                        logger.info(f"[GRAFO:{phone}] Contexto injetado: {context_type}")
                        log_event("context_detected", phone, context=context_type, ref=reference_id)
                        nd_ctx["output_data"] = _json.dumps({"contexto": context_type, "referencia": reference_id}, ensure_ascii=False)
                    else:
                        nd_ctx["output_data"] = _json.dumps({"contexto": "nenhum"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[GRAFO:{phone}] Erro ao detectar contexto: {e}", exc_info=True)
            from infra.incidentes import registrar_incidente
            registrar_incidente(phone, "contexto_falhou", str(e)[:300])
            nd_ctx["output_data"] = f"erro: {e}"

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

    # 7. Invocar grafo com retry exponencial (phone injetado via InjectedState nas tools)
    from infra.retry import invocar_com_retry
    _ai_input = {"mensagem": texto[:300] if texto else "[media]", "historico_msgs": len(lang_messages)}
    if _context_extra.get(phone):
        _ai_input["contexto_ativo"] = _context_extra[phone].get("type", "")
    async with trace_node("ai_agent", input_data=_json.dumps(_ai_input, ensure_ascii=False)) as nd_ai:
        result, last_error = await invocar_com_retry(
            graph, {"messages": lang_messages, "phone": phone}, phone=phone,
        )
        if result:
            # Extrair tools e resposta
            tool_names = []
            resposta_preview = ""
            qtd_preview = len(lang_messages)
            for m in result["messages"][qtd_preview:]:
                if isinstance(m, AIMessage):
                    if m.tool_calls:
                        for tc in m.tool_calls:
                            tool_names.append({"tool": tc["name"], "args": {k: str(v)[:100] for k, v in tc.get("args", {}).items()}})
                    elif m.content:
                        c = m.content if isinstance(m.content, str) else str(m.content)
                        if c.strip():
                            resposta_preview = c[:400]
            _ai_out = {}
            if tool_names:
                _ai_out["tool_calls"] = tool_names
            if resposta_preview:
                _ai_out["resposta"] = resposta_preview
            nd_ai["output_data"] = _json.dumps(_ai_out, ensure_ascii=False)[:1500]
        else:
            nd_ai["status"] = "error"
            nd_ai["error_message"] = str(last_error)[:300] if last_error else "no_result"

    if result is None:
        _context_extra.pop(phone, None)
        if await redis.is_paused(phone):
            logger.info(f"[GRAFO:{phone}] Pausa detectada antes do fallback — abortando")
            return
        from infra.leadbox_client import enviar_resposta_leadbox
        from infra.incidentes import registrar_incidente
        enviar_resposta_leadbox(phone, FALLBACK_MSG, queue_id=current_queue, user_id=USER_IA)
        log_event("error", phone, error=str(last_error)[:200] if last_error else "no_result")
        registrar_incidente(phone, "gemini_falhou", str(last_error)[:500] if last_error else "no_result")
        if last_error:
            _notificar_erro(phone, last_error)
        await finish_execution("error", error_msg=str(last_error)[:300] if last_error else "no_result")
        return

    # 7a. Re-check pausa pós-grafo (humano pode ter assumido durante execução do LLM)
    if await redis.is_paused(phone):
        qtd_check = len(lang_messages)
        novas_check = result["messages"][qtd_check:]
        transferiu_ok = False
        for m in novas_check:
            if isinstance(m, ToolMessage) and m.name == "transferir_departamento":
                transferiu_ok = "ERRO_TÉCNICO" not in (m.content or "")
                break
        if not transferiu_ok:
            logger.info(f"[GRAFO:{phone}] Pausa detectada pós-grafo — abortando")
            log_event("paused_post_graph", phone)
            _context_extra.pop(phone, None)
            await finish_execution("completed", "paused_post_graph")
            return

    # 7b. Extrair mensagens novas do agente (AIMessage + ToolMessage)
    qtd_enviadas = len(lang_messages)
    novas_mensagens = result["messages"][qtd_enviadas:]

    # Remover última AIMessage se tem tool_calls não-executados (route retornou END)
    if novas_mensagens and isinstance(novas_mensagens[-1], AIMessage):
        last_ai = novas_mensagens[-1]
        if last_ai.tool_calls:
            tool_call_ids = {tc.get("id") for tc in last_ai.tool_calls}
            has_response = any(
                isinstance(m, ToolMessage) and m.tool_call_id in tool_call_ids
                for m in novas_mensagens
            )
            if not has_response:
                novas_mensagens = novas_mensagens[:-1]

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

    # 8. Extrair resposta final e enviar
    # Prioridade: AIMessage SEM tool_calls > AIMessage COM tool_calls (texto + ação)
    resposta = None
    resposta_com_tool = None
    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            content = msg.content
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            content = content.strip()
            if not content or len(content) <= 2 or not content.strip('.!?…,; \n'):
                continue
            if not msg.tool_calls:
                resposta = content
                break
            elif resposta_com_tool is None:
                resposta_com_tool = content

    if not resposta and resposta_com_tool:
        resposta = resposta_com_tool

    # Logar tool calls e resposta
    for msg in novas_mensagens:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                log_event("tool_call", phone, tool=tc["name"], args={k: str(v)[:50] for k, v in tc.get("args", {}).items()})
    if resposta:
        log_event("response", phone, text=resposta[:150], tokens=usage.get("total", 0))

    # INTERCEPTOR: bloquear tool-as-text de chegar ao cliente
    from core.hallucination import detectar_tool_como_texto
    async with trace_node("guardrail", input_data=_json.dumps({"resposta_preview": resposta[:300] if resposta else ""}, ensure_ascii=False)) as nd_g:
        tool_texto = detectar_tool_como_texto(resposta) if resposta else None
        nd_g["output_data"] = _json.dumps({"bloqueado": tool_texto["tool"] if tool_texto else False, "status": "bloqueado" if tool_texto else "limpo"}, ensure_ascii=False)
    if tool_texto:
        logger.warning(f"[GRAFO:{phone}] TOOL-AS-TEXT interceptada: {tool_texto['tool']} — bloqueando envio")
        log_event("tool_as_text_blocked", phone, tool=tool_texto["tool"], text=resposta[:100])
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "tool_como_texto", f"Gemini escreveu {tool_texto['tool']} como texto", {"resposta": resposta[:300]})

        # Se era transferência, executar diretamente
        if tool_texto["tool"] == "transferir_departamento" and tool_texto.get("destino"):
            try:
                from core.tools import transferir_departamento
                result_transfer = transferir_departamento.invoke({
                    "destino": tool_texto["destino"],
                    "phone": phone,
                })
                if "Erro" in str(result_transfer):
                    logger.error(f"[GRAFO:{phone}] Interceptor: transferência falhou — {result_transfer}")
                    log_event("tool_as_text_transfer_failed", phone, tool="transferir_departamento", destino=tool_texto["destino"], error=str(result_transfer)[:200])
                    from infra.leadbox_client import enviar_resposta_leadbox
                    enviar_resposta_leadbox(phone, FALLBACK_MSG, queue_id=current_queue, user_id=USER_IA)
                else:
                    logger.info(f"[GRAFO:{phone}] Transferência executada via interceptor: {tool_texto['destino']} → {result_transfer}")
                    log_event("tool_as_text_recovered", phone, tool="transferir_departamento", destino=tool_texto["destino"])
            except Exception as e:
                logger.error(f"[GRAFO:{phone}] Falha ao executar transferência via interceptor: {e}", exc_info=True)
                registrar_incidente(phone, "transferencia_falhou", f"Interceptor exception: {e}"[:300], {"destino": tool_texto.get("destino")})
                from infra.leadbox_client import enviar_resposta_leadbox
                enviar_resposta_leadbox(phone, FALLBACK_MSG, queue_id=current_queue, user_id=USER_IA)
        else:
            # Outra tool como texto → fallback genérico + transferir para humano
            from infra.leadbox_client import enviar_resposta_leadbox
            enviar_resposta_leadbox(phone, FALLBACK_MSG, queue_id=current_queue, user_id=USER_IA)

        # Limpar contexto e sair (não enviar resposta original)
        _context_extra.pop(phone, None)
        await finish_execution("completed", "tool_as_text_intercepted")
        return

    # Auto-snooze 48h: se era contexto billing e Ana NÃO transferiu, silencia disparos
    from core.auto_snooze import auto_snooze_billing
    ctx_data = _context_extra.get(phone)
    ctx_type = ctx_data["type"] if ctx_data else None
    await auto_snooze_billing(phone, ctx_type, novas_mensagens, redis)

    # Limpar cache de contexto
    _context_extra.pop(phone, None)

    # Re-check pausa antes de enviar (humano pode ter assumido durante processamento)
    # Exceção: se a própria Ana transferiu COM SUCESSO nesta invocação, enviar despedida
    ia_transferiu = False
    for i, m in enumerate(novas_mensagens):
        if isinstance(m, AIMessage) and m.tool_calls:
            if any(tc["name"] == "transferir_departamento" for tc in m.tool_calls):
                for m2 in novas_mensagens[i+1:]:
                    if isinstance(m2, ToolMessage) and m2.name == "transferir_departamento":
                        ia_transferiu = "ERRO_TÉCNICO" not in (m2.content or "")
                        break
                break

    if await redis.is_paused(phone) and not ia_transferiu:
        logger.info(f"[GRAFO:{phone}] Pausa detectada antes do envio — abortando resposta")
        log_event("paused_before_send", phone)
        await finish_execution("completed", "paused_before_send")
        return

    if ia_transferiu:
        logger.info(f"[GRAFO:{phone}] Transferência com sucesso — enviando despedida antes de pausar")

    # Enviar resposta via Leadbox
    # Se a Ana transferiu, NÃO enviar queue_id/user_id na despedida — senão
    # forceTicketToDepartment desfaz a transferência movendo o ticket de volta pra fila IA
    from infra.leadbox_client import enviar_resposta_leadbox

    send_queue = None if ia_transferiu else current_queue
    send_user = None if ia_transferiu else USER_IA

    async with trace_node("enviar_resposta", input_data=_json.dumps({"resposta": resposta[:300] if resposta else "fallback", "fila_envio": send_queue, "usuario": send_user}, ensure_ascii=False)) as nd_env:
        if resposta:
            enviar_resposta_leadbox(phone, resposta, queue_id=send_queue, user_id=send_user)
            nd_env["output_data"] = _json.dumps({"status": "enviado", "canal": "leadbox"}, ensure_ascii=False)
        else:
            from infra.incidentes import registrar_incidente
            registrar_incidente(phone, "resposta_vazia", "Gemini retornou sem texto")
            enviar_resposta_leadbox(phone, FALLBACK_MSG, queue_id=send_queue, user_id=send_user)
            nd_env["output_data"] = _json.dumps({"status": "fallback", "motivo": "resposta_vazia"}, ensure_ascii=False)

    # Salvar todas as mensagens do agente APÓS filtro e envio
    async with trace_node("salvar_historico", input_data=_json.dumps({"mensagens_agente": len(mensagens_agente)}, ensure_ascii=False)) as nd_s:
        if mensagens_agente:
            salvar_mensagens_agente(phone, mensagens_agente, usage=usage or None)
            nd_s["output_data"] = _json.dumps({"salvas": len(mensagens_agente)}, ensure_ascii=False)
        else:
            nd_s["output_data"] = _json.dumps({"salvas": 0, "motivo": "nenhuma msg agente"}, ensure_ascii=False)

    await finish_execution("completed", output_preview=resposta[:200] if resposta else "fallback")
