"""Testes unitários para core/hallucination.py — detectar_tool_como_texto."""

import pytest
from core.hallucination import detectar_tool_como_texto


class TestDetectarToolComoTexto:
    """Testa a detecção de tool escrita como texto pelo Gemini."""

    def test_transferir_com_args_completos(self):
        """String com chamada literal completa deve retornar tool + args."""
        resultado = detectar_tool_como_texto(
            "transferir_departamento(queue_id=453, user_id=815)"
        )
        assert resultado is not None
        assert resultado["tool"] == "transferir_departamento"
        assert resultado["queue_id"] == 453
        assert resultado["user_id"] == 815

    def test_transferir_financeiro(self):
        """Transferência para financeiro com IDs diferentes."""
        resultado = detectar_tool_como_texto(
            "transferir_departamento(queue_id=454, user_id=814)"
        )
        assert resultado is not None
        assert resultado["queue_id"] == 454
        assert resultado["user_id"] == 814

    def test_consultar_cliente_como_texto(self):
        """consultar_cliente escrita como texto deve ser detectada."""
        resultado = detectar_tool_como_texto(
            "consultar_cliente(cpf='12345678901')"
        )
        assert resultado is not None
        assert resultado["tool"] == "consultar_cliente"

    def test_registrar_compromisso_como_texto(self):
        """registrar_compromisso escrita como texto deve ser detectada."""
        resultado = detectar_tool_como_texto(
            "registrar_compromisso(data_prometida='2026-04-15')"
        )
        assert resultado is not None
        assert resultado["tool"] == "registrar_compromisso"

    def test_texto_normal_sem_tool(self):
        """Resposta normal sem nome de tool deve retornar None."""
        assert detectar_tool_como_texto("Olá! Como posso te ajudar?") is None

    def test_texto_com_transferencia_natural(self):
        """Texto que menciona 'transferir' sem sintaxe de função."""
        assert detectar_tool_como_texto(
            "Vou te transferir para o atendimento, só um momento."
        ) is None

    def test_texto_com_nome_da_tool_sem_parenteses(self):
        """Nome da tool sem parênteses NÃO deve ser detectado."""
        assert detectar_tool_como_texto(
            "Vou usar transferir_departamento para te ajudar"
        ) is None

    def test_string_vazia(self):
        """String vazia retorna None."""
        assert detectar_tool_como_texto("") is None

    def test_none(self):
        """None retorna None."""
        assert detectar_tool_como_texto(None) is None

    def test_tool_com_espacos(self):
        """Tool com espaços antes dos parênteses."""
        resultado = detectar_tool_como_texto(
            "transferir_departamento (queue_id=453, user_id=815)"
        )
        assert resultado is not None
        assert resultado["tool"] == "transferir_departamento"

    def test_tool_dentro_de_frase(self):
        """Tool embutida no meio de uma frase."""
        resultado = detectar_tool_como_texto(
            "Ok, vou chamar transferir_departamento(queue_id=453, user_id=815) agora."
        )
        assert resultado is not None
        assert resultado["queue_id"] == 453

    def test_transferir_sem_args(self):
        """Tool com parênteses mas sem args extraíveis."""
        resultado = detectar_tool_como_texto(
            "transferir_departamento()"
        )
        assert resultado is not None
        assert resultado["tool"] == "transferir_departamento"
        assert "queue_id" not in resultado
