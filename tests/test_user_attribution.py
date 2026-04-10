"""Testes de atribuição de lead ao user Ana (1095) nas filas IA.

Valida que processar_mensagens passa queue_id e user_id corretos
ao enviar resposta via Leadbox, conforme a fila atual do lead.
"""

import asyncio
import os
import sys
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.constants import QUEUE_IA, QUEUE_BILLING, QUEUE_MANUTENCAO, USER_IA


# ── Helpers ──

def _make_supabase_mock(queue_id=None, state="ai"):
    """Cria mock do Supabase que retorna lead com queue_id e state."""
    mock_sb = MagicMock()
    lead_data = {"current_queue_id": queue_id, "current_state": state, "conversation_history": None}
    mock_sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[lead_data]
    )
    return mock_sb


def _make_redis_mock(paused=False):
    """Cria mock do RedisService."""
    mock_redis = AsyncMock()
    mock_redis.is_paused = AsyncMock(return_value=paused)
    mock_redis.pause_set = AsyncMock()
    mock_redis.lock_acquire = AsyncMock(return_value=True)
    mock_redis.lock_release = AsyncMock()
    mock_redis.client = AsyncMock()
    return mock_redis


def _make_graph_result(text="Olá! Sou a Ana da Aluga-Ar."):
    """Cria resultado fake do graph.ainvoke com AIMessage."""
    from langchain_core.messages import AIMessage, HumanMessage
    return {
        "messages": [
            HumanMessage(content="Oi"),
            AIMessage(content=text),
        ]
    }


async def _run_processar(queue_id, state="ai", paused=False):
    """Executa processar_mensagens com mocks e retorna mock do enviar_resposta_leadbox."""
    mock_sb = _make_supabase_mock(queue_id=queue_id, state=state)
    mock_redis = _make_redis_mock(paused=paused)
    mock_enviar = MagicMock(return_value=True)

    with patch("infra.redis.get_redis_service", AsyncMock(return_value=mock_redis)), \
         patch("infra.supabase.get_supabase", return_value=mock_sb), \
         patch("infra.nodes_supabase.buscar_historico", return_value=[]), \
         patch("infra.nodes_supabase.salvar_mensagem"), \
         patch("infra.nodes_supabase.salvar_mensagens_agente"), \
         patch("infra.event_logger.log_event"), \
         patch("core.context_detector.detect_context", return_value=(None, None)), \
         patch("core.hallucination.detectar_hallucination", return_value=[]), \
         patch("core.auto_snooze.auto_snooze_billing", AsyncMock()), \
         patch("infra.retry.invocar_com_retry", AsyncMock(return_value=(_make_graph_result(), None))), \
         patch("infra.leadbox_client.enviar_resposta_leadbox", mock_enviar):
        from core.grafo import processar_mensagens
        await processar_mensagens("5565999990000", [{"texto": "Oi"}])

    return mock_enviar


# ── Testes ──

@pytest.mark.asyncio
async def test_atribuicao_fila_ia_537():
    """Lead na fila IA (537) → resposta com queue_id=537, user_id=1095."""
    mock_enviar = await _run_processar(queue_id=537)
    mock_enviar.assert_called_once()
    _, kwargs = mock_enviar.call_args
    assert kwargs.get("queue_id") == QUEUE_IA
    assert kwargs.get("user_id") == USER_IA


@pytest.mark.asyncio
async def test_atribuicao_fila_billing_544():
    """Lead na fila billing (544) → resposta com queue_id=544, user_id=1095."""
    mock_enviar = await _run_processar(queue_id=544)
    mock_enviar.assert_called_once()
    _, kwargs = mock_enviar.call_args
    assert kwargs.get("queue_id") == QUEUE_BILLING
    assert kwargs.get("user_id") == USER_IA


@pytest.mark.asyncio
async def test_atribuicao_fila_manutencao_545():
    """Lead na fila manutenção (545) → resposta com queue_id=545, user_id=1095."""
    mock_enviar = await _run_processar(queue_id=545)
    mock_enviar.assert_called_once()
    _, kwargs = mock_enviar.call_args
    assert kwargs.get("queue_id") == QUEUE_MANUTENCAO
    assert kwargs.get("user_id") == USER_IA


@pytest.mark.asyncio
async def test_lead_novo_sem_queue_usa_default():
    """Lead novo (queue=None) → resposta com queue_id=537 (default QUEUE_IA)."""
    mock_enviar = await _run_processar(queue_id=None)
    mock_enviar.assert_called_once()
    _, kwargs = mock_enviar.call_args
    assert kwargs.get("queue_id") == QUEUE_IA
    assert kwargs.get("user_id") == USER_IA


@pytest.mark.asyncio
async def test_lead_pausado_nao_envia():
    """Lead pausado por humano → IA não processa, enviar_resposta NÃO chamado."""
    mock_enviar = await _run_processar(queue_id=537, paused=True)
    mock_enviar.assert_not_called()


def test_alerta_admin_sem_atribuicao():
    """Alerta admin (_notificar_erro) NÃO deve passar queue_id/user_id."""
    mock_enviar = MagicMock(return_value=True)

    with patch("infra.leadbox_client.enviar_resposta_leadbox", mock_enviar), \
         patch("core.grafo.ADMIN_PHONE", "5565000000000"):
        from core.grafo import _notificar_erro
        _notificar_erro("5565999990000", Exception("teste"))

    mock_enviar.assert_called_once()
    call_kwargs = mock_enviar.call_args.kwargs
    assert "queue_id" not in call_kwargs
    assert "user_id" not in call_kwargs
    # Primeiro arg deve ser o ADMIN_PHONE, não o phone do lead
    assert mock_enviar.call_args.args[0] == "5565000000000"
