"""Testes de integração do guardrail antierro no call_model().

Testa o fluxo completo: LLM responde com hallucination → guardrail detecta →
retry → LLM corrige (ou fallback). Usa mock do LLM para controlar respostas.
"""

import pytest
from unittest.mock import AsyncMock, patch
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from core.constants import FALLBACK_MSG


# --- Helpers ---

def _make_state(messages, phone="5565999990000"):
    return {"messages": messages, "phone": phone}


def _ai_msg(content, tool_calls=None):
    return AIMessage(content=content, tool_calls=tool_calls or [])


def _tool_msg(name, content="ok"):
    return ToolMessage(content=content, name=name, tool_call_id="fake")


# --- Testes ---

@pytest.mark.asyncio
async def test_guardrail_hallucination_retry_corrige_com_tool():
    """
    CENÁRIO PRINCIPAL: Gemini diz 'transferi' sem tool → guardrail detecta →
    retry → Gemini chama transferir_departamento no retry.
    """
    from core.grafo import call_model

    resposta_mentirosa = _ai_msg("Já transferi você para o financeiro.")
    resposta_corrigida = _ai_msg("", tool_calls=[{
        "name": "transferir_departamento",
        "args": {"destino": "financeiro"},
        "id": "retry_1",
    }])

    state = _make_state([HumanMessage(content="quero falar com o financeiro")])

    with patch("core.grafo.get_model") as mock_model:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[resposta_mentirosa, resposta_corrigida])
        mock_model.return_value = mock_llm

        result = await call_model(state)

    final_msg = result["messages"][0]
    assert final_msg.tool_calls, "Retry deveria ter retornado tool_calls"
    assert final_msg.tool_calls[0]["name"] == "transferir_departamento"
    assert mock_llm.ainvoke.call_count == 2


@pytest.mark.asyncio
async def test_guardrail_hallucination_retry_falha_fallback():
    """
    Gemini diz 'transferi' sem tool → retry → retry TAMBÉM não chama tool →
    contingência: transferência forçada via tool_call sintético.
    """
    from core.grafo import call_model

    resposta_mentirosa = _ai_msg("Já transferi você para o financeiro.")
    resposta_retry_ruim = _ai_msg("Pronto, já encaminhei para o financeiro!")

    state = _make_state([HumanMessage(content="quero falar com o financeiro")])

    with patch("core.grafo.get_model") as mock_model:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[resposta_mentirosa, resposta_retry_ruim])
        mock_model.return_value = mock_llm

        result = await call_model(state)

    final_msg = result["messages"][0]
    # Para transferir_departamento: contingência cria tool_call sintético
    # OU fallback com FALLBACK_MSG — ambos são aceitáveis (mentira NÃO chega)
    content = str(final_msg.content).lower() if final_msg.content else ""
    has_tool_call = bool(final_msg.tool_calls)
    has_fallback = FALLBACK_MSG.lower() in content
    assert has_tool_call or has_fallback, "Mentira não deveria chegar ao cliente"
    assert "transferi" not in content, "Resposta mentirosa original não deveria passar"


@pytest.mark.asyncio
async def test_guardrail_sem_hallucination_passa_direto():
    """
    Gemini responde texto normal sem afirmar ação → guardrail NÃO interfere.
    """
    from core.grafo import call_model

    resposta_normal = _ai_msg("Olá! Como posso te ajudar?")
    state = _make_state([HumanMessage(content="oi")])

    with patch("core.grafo.get_model") as mock_model:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=resposta_normal)
        mock_model.return_value = mock_llm

        result = await call_model(state)

    assert result["messages"][0].content == "Olá! Como posso te ajudar?"
    assert mock_llm.ainvoke.call_count == 1


@pytest.mark.asyncio
async def test_guardrail_tool_call_original_passa_direto():
    """
    Gemini já retorna tool_calls na primeira chamada → guardrail NÃO roda.
    """
    from core.grafo import call_model

    resposta_com_tool = _ai_msg("", tool_calls=[{
        "name": "consultar_cliente",
        "args": {"cpf": "12345678900"},
        "id": "tc_1",
    }])
    state = _make_state([HumanMessage(content="meu cpf é 123.456.789-00")])

    with patch("core.grafo.get_model") as mock_model:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=resposta_com_tool)
        mock_model.return_value = mock_llm

        result = await call_model(state)

    assert result["messages"][0].tool_calls[0]["name"] == "consultar_cliente"
    assert mock_llm.ainvoke.call_count == 1


@pytest.mark.asyncio
async def test_guardrail_tool_ja_chamada_na_sessao():
    """
    Ana diz 'verifiquei' MAS consultar_cliente JÁ foi chamada nesta sessão →
    NÃO é hallucination (ela realmente verificou).
    """
    from core.grafo import call_model

    messages = [
        HumanMessage(content="meu cpf é 123"),
        _ai_msg("", tool_calls=[{"name": "consultar_cliente", "args": {"cpf": "123"}, "id": "tc1"}]),
        _tool_msg("consultar_cliente", "Cliente: João, 2 cobranças PENDING"),
    ]

    resposta_final = _ai_msg("Verifiquei aqui e encontrei 2 cobranças pendentes.")
    state = _make_state(messages)

    with patch("core.grafo.get_model") as mock_model:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=resposta_final)
        mock_model.return_value = mock_llm

        result = await call_model(state)

    assert result["messages"][0].content == "Verifiquei aqui e encontrei 2 cobranças pendentes."
    assert mock_llm.ainvoke.call_count == 1


@pytest.mark.asyncio
async def test_guardrail_max_1_retry():
    """
    Garante que retry roda NO MÁXIMO 1 vez, mesmo que continue falhando.
    Previne loop infinito.
    """
    from core.grafo import call_model

    resposta_mentirosa_1 = _ai_msg("Registrei seu compromisso para sexta.")
    resposta_mentirosa_2 = _ai_msg("Anotei o compromisso para sexta-feira.")

    state = _make_state([HumanMessage(content="pode anotar pra sexta")])

    with patch("core.grafo.get_model") as mock_model:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[resposta_mentirosa_1, resposta_mentirosa_2])
        mock_model.return_value = mock_llm

        result = await call_model(state)

    # Deve ter chamado LLM exatamente 2x (original + 1 retry), nunca 3
    assert mock_llm.ainvoke.call_count == 2
    # Resposta final deve ser fallback (não a mentira)
    final_content = str(result["messages"][0].content)
    assert final_content == FALLBACK_MSG


# ── Testes de persistência: mentira NUNCA chega ao Supabase ──

@pytest.mark.asyncio
async def test_mentira_nao_entra_no_state_do_grafo():
    """
    Prova que a resposta mentirosa NUNCA entra no State do LangGraph,
    portanto nunca será salva por salvar_mensagens_agente().

    Simula o fluxo completo: graph.ainvoke() → extrai novas_mensagens →
    verifica que nenhuma delas contém a mentira.
    """
    from core.grafo import call_model

    MENTIRA = "Já transferi você para o financeiro."
    resposta_mentirosa = _ai_msg(MENTIRA)
    resposta_corrigida = _ai_msg("", tool_calls=[{
        "name": "transferir_departamento",
        "args": {"destino": "financeiro"},
        "id": "retry_1",
    }])

    msg_cliente = HumanMessage(content="quero falar com o financeiro")
    state = _make_state([msg_cliente])

    with patch("core.grafo.get_model") as mock_model:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[resposta_mentirosa, resposta_corrigida])
        mock_model.return_value = mock_llm

        result = await call_model(state)

    # Simular extração de novas_mensagens como processar_mensagens() faz (L468-469)
    # State inicial tinha 1 msg (msg_cliente). O que call_model retorna é adicionado.
    mensagens_retornadas = result["messages"]

    # PROVA 1: nenhuma mensagem retornada contém a mentira
    for msg in mensagens_retornadas:
        content = str(msg.content) if msg.content else ""
        assert MENTIRA not in content, \
            f"Mentira encontrada no retorno de call_model: {content}"

    # PROVA 2: nenhuma mensagem retornada contém a instrução de correção do sistema
    for msg in mensagens_retornadas:
        content = str(msg.content) if msg.content else ""
        assert "CORREÇÃO OBRIGATÓRIA" not in content, \
            f"Mensagem de correção do sistema vazou para o State: {content}"

    # PROVA 3: a mensagem que seria salva é a corrigida (tool_call)
    assert mensagens_retornadas[0].tool_calls[0]["name"] == "transferir_departamento"


@pytest.mark.asyncio
async def test_mentira_nao_entra_no_state_fallback():
    """
    Quando retry também falha e vai pro fallback, a mentira
    original E a mentira do retry NÃO entram no State.
    O que entra é FALLBACK_MSG.
    """
    from core.grafo import call_model

    MENTIRA_1 = "Registrei seu compromisso para sexta."
    MENTIRA_2 = "Anotei o compromisso para sexta-feira."

    state = _make_state([HumanMessage(content="pode anotar pra sexta")])

    with patch("core.grafo.get_model") as mock_model:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[_ai_msg(MENTIRA_1), _ai_msg(MENTIRA_2)])
        mock_model.return_value = mock_llm

        result = await call_model(state)

    mensagens_retornadas = result["messages"]

    # PROVA: nenhuma mentira no retorno
    for msg in mensagens_retornadas:
        content = str(msg.content) if msg.content else ""
        assert MENTIRA_1 not in content, f"Mentira 1 vazou: {content}"
        assert MENTIRA_2 not in content, f"Mentira 2 vazou: {content}"
        assert "CORREÇÃO OBRIGATÓRIA" not in content, f"Msg sistema vazou: {content}"

    # O que foi retornado é o FALLBACK_MSG
    assert str(mensagens_retornadas[0].content) == FALLBACK_MSG
