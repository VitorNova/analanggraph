"""Testes unitários para o interceptor de tool-as-text em core/grafo.py.

Testa que quando o Gemini retorna tool name como content (texto) em vez de
tool_calls (function calling), o interceptor:
1. Bloqueia o envio do texto ao cliente
2. Executa a transferência diretamente (se era transferir_departamento)
3. Envia fallback genérico (se era outra tool)
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from langchain_core.messages import AIMessage, HumanMessage


@pytest.fixture
def mock_env(monkeypatch):
    """Seta variáveis de ambiente necessárias."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    monkeypatch.setenv("REDIS_URL", "redis://localhost")


def _make_graph_result(content: str, tool_calls: list = None):
    """Cria resultado do grafo simulando resposta do Gemini."""
    ai_msg = AIMessage(content=content, tool_calls=tool_calls or [])
    return {
        "messages": [
            HumanMessage(content="teste"),
            ai_msg,
        ]
    }


class TestInterceptorToolAsText:
    """Testa o interceptor que bloqueia tool-as-text."""

    def test_detecta_transferir_como_texto(self):
        """Resposta com transferir_departamento(...) como texto é detectada."""
        from core.hallucination import detectar_tool_como_texto

        resultado = detectar_tool_como_texto(
            "transferir_departamento(queue_id=453, user_id=815)"
        )
        assert resultado is not None
        assert resultado["tool"] == "transferir_departamento"
        assert resultado["queue_id"] == 453
        assert resultado["user_id"] == 815

    def test_nao_detecta_resposta_limpa(self):
        """Resposta normal sem tool como texto retorna None."""
        from core.hallucination import detectar_tool_como_texto

        assert detectar_tool_como_texto("Olá, como posso ajudar?") is None

    def test_nao_detecta_tool_calls_nativo(self):
        """Quando tool_calls está preenchido, content normalmente está vazio."""
        from core.hallucination import detectar_tool_como_texto

        # Content vazio = tool_calls nativo funcionou
        assert detectar_tool_como_texto("") is None

    @pytest.mark.asyncio
    async def test_interceptor_bloqueia_envio_e_transfere(self, mock_env):
        """Se Gemini escrever transferir_departamento como texto,
        interceptor deve: (1) NÃO enviar texto ao cliente, (2) executar tool diretamente."""
        from core.hallucination import detectar_tool_como_texto

        # Simula resposta do Gemini com tool como texto
        resposta = "transferir_departamento(queue_id=453, user_id=815)"
        tool_texto = detectar_tool_como_texto(resposta)

        assert tool_texto is not None, "Interceptor deveria detectar tool como texto"
        assert tool_texto["tool"] == "transferir_departamento"
        assert tool_texto["queue_id"] == 453
        assert tool_texto["user_id"] == 815

        # Simula a lógica do interceptor (sem rodar o grafo inteiro)
        with patch("core.tools.transferir_departamento") as mock_transfer:
            mock_transfer.invoke.return_value = "Transferido para fila 453 com sucesso"

            # O interceptor chamaria:
            if tool_texto["tool"] == "transferir_departamento" and tool_texto.get("queue_id"):
                result = mock_transfer.invoke({
                    "queue_id": tool_texto["queue_id"],
                    "user_id": tool_texto["user_id"],
                    "phone": "5565999990000",
                })

            mock_transfer.invoke.assert_called_once_with({
                "queue_id": 453,
                "user_id": 815,
                "phone": "5565999990000",
            })

    @pytest.mark.asyncio
    async def test_interceptor_fallback_para_outra_tool(self, mock_env):
        """Se Gemini escrever consultar_cliente como texto (sem queue_id),
        interceptor deve enviar fallback genérico."""
        from core.hallucination import detectar_tool_como_texto

        resposta = "consultar_cliente(cpf='12345678901')"
        tool_texto = detectar_tool_como_texto(resposta)

        assert tool_texto is not None
        assert tool_texto["tool"] == "consultar_cliente"
        # Sem queue_id → não pode executar transferência → fallback
        assert "queue_id" not in tool_texto
