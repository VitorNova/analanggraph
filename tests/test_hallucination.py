"""Teste unitário do detector de hallucination (core/hallucination.py).

Valida que a regex com \\b diferencia corretamente:
- "transferi" (passado → hallucination) vs "transferir" (infinitivo → ok)
- "encaminhei" vs "encaminhar"
- "verifiquei" vs "verificar"

Testa tanto a função pública `detectar_hallucination` (com AIMessage reais)
quanto os patterns regex diretamente.
"""

import re
from unittest.mock import MagicMock

from core.hallucination import detectar_hallucination, inferir_destino_do_texto, _HALL_CHECKS


# ── Helpers ──

def _detecta(resposta: str, tool_name: str) -> bool:
    """Testa regex diretamente (sem AIMessage)."""
    resp_lower = resposta.lower()
    for tn, frases in _HALL_CHECKS:
        if tn == tool_name:
            return any(re.search(f, resp_lower) for f in frases)
    return False


def _make_ai_message(content: str, tool_calls=None):
    """Cria AIMessage mock com content e tool_calls."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    type(msg).__name__ = "AIMessage"
    # Para isinstance check funcionar
    return msg


def _patch_isinstance():
    """Patch para isinstance funcionar com mocks no detectar_hallucination."""
    from langchain_core.messages import AIMessage
    return AIMessage


# ── Testes de Regex (unitários puros) ──

def test_hallucination_real_transferi():
    assert _detecta("Já transferi você para o financeiro.", "transferir_departamento")


def test_falso_positivo_transferir():
    assert not _detecta("Posso te transferir para o financeiro?", "transferir_departamento")


def test_vou_transferir_nao_detecta():
    # "Vou te transferir" removido dos patterns — ambíguo demais (falsos positivos no funil de vendas)
    # Detecção agora se limita a tempo passado inequívoco ("transferi", "encaminhei")
    assert not _detecta("Vou te transferir para o financeiro, pode ser?", "transferir_departamento")


def test_hallucination_encaminhei():
    assert _detecta("Encaminhei seu caso para o atendimento.", "transferir_departamento")


def test_falso_positivo_encaminhar():
    assert not _detecta("Preciso encaminhar para o financeiro.", "transferir_departamento")


def test_hallucination_te_passo_para():
    assert _detecta("Te passo para o financeiro agora.", "transferir_departamento")


def test_falso_positivo_direcionando():
    assert not _detecta("Estou direcionando seu atendimento.", "transferir_departamento")


def test_hallucination_registrei():
    assert _detecta("Registrei seu compromisso para sexta.", "registrar_compromisso")


def test_falso_positivo_registrar():
    assert not _detecta("Posso registrar um compromisso?", "registrar_compromisso")


def test_hallucination_compromisso_registrado():
    assert _detecta("Compromisso registrado para dia 10.", "registrar_compromisso")


def test_hallucination_verifiquei():
    assert _detecta("Verifiquei aqui e seu pagamento consta.", "consultar_cliente")


def test_falso_positivo_verificar():
    assert not _detecta("Vou verificar seu pagamento.", "consultar_cliente")


def test_hallucination_consultei():
    assert _detecta("Consultei e encontrei 2 faturas.", "consultar_cliente")


def test_falso_positivo_consultar():
    assert not _detecta("Preciso consultar seu CPF.", "consultar_cliente")


def test_hallucination_encontrei_no_sistema():
    assert _detecta("Encontrei no sistema suas cobranças.", "consultar_cliente")


def test_falso_positivo_encontrei_generico():
    assert not _detecta("Não encontrei nada com esse CPF.", "consultar_cliente")


# ── Testes da função detectar_hallucination (integração com AIMessage) ──

def test_detectar_hallucination_com_tool_chamada():
    """Se tool foi chamada, NÃO é hallucination mesmo com texto."""
    from langchain_core.messages import AIMessage

    msg_com_tool = AIMessage(content="", tool_calls=[{"name": "transferir_departamento", "args": {}, "id": "1"}])
    msg_resposta = AIMessage(content="Já transferi você para o financeiro.")
    result = detectar_hallucination([msg_com_tool, msg_resposta], "5565999990000")
    assert "transferir_departamento" not in result


def test_detectar_hallucination_sem_tool_chamada():
    """Se tool NÃO foi chamada mas texto afirma, É hallucination."""
    from langchain_core.messages import AIMessage

    msg = AIMessage(content="Já transferi você para o financeiro.")
    result = detectar_hallucination([msg], "5565999990000")
    assert "transferir_departamento" in result


def test_detectar_hallucination_texto_limpo():
    """Texto normal sem afirmação de ação → sem hallucination."""
    from langchain_core.messages import AIMessage

    msg = AIMessage(content="Olá! Como posso te ajudar?")
    result = detectar_hallucination([msg], "5565999990000")
    assert result == []


def test_detectar_hallucination_mensagem_vazia():
    """Lista vazia → sem hallucination."""
    result = detectar_hallucination([], "5565999990000")
    assert result == []


# ── Testes de inferir_destino_do_texto (contingência hallucination) ──

def test_inferir_destino_financeiro():
    assert inferir_destino_do_texto("Vou transferir para o financeiro verificar") == "financeiro"


def test_inferir_destino_atendimento():
    assert inferir_destino_do_texto("Já encaminhei para o atendimento") == "atendimento"


def test_inferir_destino_cobrancas():
    assert inferir_destino_do_texto("Te transferi para cobranças") == "cobrancas"


def test_inferir_destino_caso_real_575503():
    """Caso real de produção 2026-04-13: 'Vou transferir para lá!' sem setor explícito."""
    result = inferir_destino_do_texto(
        "Como você já efetuou o pagamento, preciso que encaminhe o comprovante para o financeiro verificar, tudo bem? Vou transferir para lá!"
    )
    assert result == "financeiro"


def test_inferir_destino_texto_sem_transferencia():
    assert inferir_destino_do_texto("Olá! Como posso te ajudar?") is None


def test_inferir_destino_fallback_atendimento():
    """Texto com 'transferir' mas sem setor → fallback atendimento."""
    assert inferir_destino_do_texto("Vou transferir para lá!") == "atendimento"


def test_inferir_destino_none():
    assert inferir_destino_do_texto(None) is None


def test_inferir_destino_vazio():
    assert inferir_destino_do_texto("") is None


# ── Testes de falsos positivos do funil de vendas (fix 2026-04-27) ──

def test_falso_positivo_funil_vendas_encaminho():
    """Caso real de produção: Ana pede CPF e diz 'já te encaminho' — NÃO é hallucination."""
    assert not _detecta(
        "Me passa nome e CPF, já te encaminho pro time finalizar.",
        "transferir_departamento",
    )


def test_falso_positivo_funil_vendas_transfiro():
    """Ana diz 'te transfiro' no futuro condicional — NÃO deve detectar."""
    assert not _detecta(
        "Com esses dados, eu já te transfiro pra Nathália.",
        "transferir_departamento",
    )


def test_falso_positivo_vou_transferir_condicional():
    """'Vou transferir' condicional — NÃO deve detectar (removido dos patterns)."""
    assert not _detecta(
        "Assim que me passar os dados, vou transferir pro atendimento.",
        "transferir_departamento",
    )


def test_verdadeiro_positivo_encaminhei_passado():
    """'Encaminhei' no passado SEM tool call — DEVE detectar."""
    assert _detecta(
        "Já encaminhei sua solicitação para a Nathália.",
        "transferir_departamento",
    )


def test_verdadeiro_positivo_transferi_passado():
    """'Transferi' no passado SEM tool call — DEVE detectar."""
    assert _detecta(
        "Transferi você para o financeiro.",
        "transferir_departamento",
    )


# ── Testes de checar_resposta_pre_envio (guardrail preventivo) ──

from core.hallucination import checar_resposta_pre_envio


# --- Caso 1: Detecta hallucination quando tool NÃO foi chamada ---

def test_pre_envio_detecta_transferi_sem_tool():
    """Ana diz 'transferi' sem ter chamado transferir_departamento → violação."""
    violations = checar_resposta_pre_envio(
        "já transferi você para o financeiro.",
        tool_names_in_session=set(),
    )
    assert len(violations) >= 1
    assert violations[0][0] == "transferir_departamento"


def test_pre_envio_detecta_registrei_sem_tool():
    """Ana diz 'registrei' sem ter chamado registrar_compromisso → violação."""
    violations = checar_resposta_pre_envio(
        "registrei seu compromisso para sexta-feira.",
        tool_names_in_session=set(),
    )
    assert len(violations) >= 1
    assert violations[0][0] == "registrar_compromisso"


def test_pre_envio_detecta_verifiquei_sem_tool():
    """Ana diz 'verifiquei' sem ter chamado consultar_cliente → violação."""
    violations = checar_resposta_pre_envio(
        "verifiquei aqui e seu pagamento consta.",
        tool_names_in_session=set(),
    )
    assert len(violations) >= 1
    assert violations[0][0] == "consultar_cliente"


# --- Caso 2: NÃO detecta quando tool FOI chamada na sessão ---

def test_pre_envio_permite_transferi_com_tool():
    """Ana diz 'transferi' E a tool foi chamada → sem violação."""
    violations = checar_resposta_pre_envio(
        "já transferi você para o financeiro.",
        tool_names_in_session={"transferir_departamento"},
    )
    assert len(violations) == 0


def test_pre_envio_permite_registrei_com_tool():
    """Ana diz 'registrei' E a tool foi chamada → sem violação."""
    violations = checar_resposta_pre_envio(
        "registrei seu compromisso para sexta-feira.",
        tool_names_in_session={"registrar_compromisso"},
    )
    assert len(violations) == 0


def test_pre_envio_permite_verifiquei_com_tool():
    """Ana diz 'verifiquei' E a tool foi chamada → sem violação."""
    violations = checar_resposta_pre_envio(
        "verifiquei aqui e seu pagamento consta.",
        tool_names_in_session={"consultar_cliente"},
    )
    assert len(violations) == 0


# --- Caso 3: Texto limpo → sem violação ---

def test_pre_envio_texto_limpo():
    """Saudação normal sem afirmação de ação → sem violação."""
    violations = checar_resposta_pre_envio(
        "olá! como posso te ajudar?",
        tool_names_in_session=set(),
    )
    assert violations == []


def test_pre_envio_texto_vazio():
    """String vazia → sem violação."""
    violations = checar_resposta_pre_envio("", tool_names_in_session=set())
    assert violations == []


# --- Caso 4: Falsos positivos do funil de vendas ---

def test_pre_envio_nao_detecta_futuro_transferir():
    """'Vou te transferir' (futuro) NÃO é hallucination — ainda não afirmou que fez."""
    violations = checar_resposta_pre_envio(
        "com esses dados, vou te transferir para a nathália.",
        tool_names_in_session=set(),
    )
    # Nenhuma violação de transferir_departamento (futuro, não passado)
    assert all(v[0] != "transferir_departamento" for v in violations)


def test_pre_envio_nao_detecta_infinitivo():
    """'Posso consultar' (infinitivo) NÃO é hallucination."""
    violations = checar_resposta_pre_envio(
        "preciso consultar seu cpf antes de verificar.",
        tool_names_in_session=set(),
    )
    assert all(v[0] != "consultar_cliente" for v in violations)


# --- Caso 5: Content como string simples ---

def test_pre_envio_content_string():
    """Content como string simples funciona."""
    violations = checar_resposta_pre_envio(
        "já transferi para o atendimento",
        tool_names_in_session=set(),
    )
    assert len(violations) >= 1


# --- Caso 6: Múltiplas violações simultâneas ---

def test_pre_envio_multiplas_violacoes():
    """Ana afirma ter feito 2 ações sem chamar nenhuma tool."""
    violations = checar_resposta_pre_envio(
        "verifiquei no sistema e já transferi para o financeiro.",
        tool_names_in_session=set(),
    )
    tool_names = [v[0] for v in violations]
    assert "consultar_cliente" in tool_names
    assert "transferir_departamento" in tool_names


# --- Caso 7: Tool parcialmente chamada ---

def test_pre_envio_uma_tool_chamada_outra_nao():
    """consultar_cliente chamada, mas transferir_departamento não → só 1 violação."""
    violations = checar_resposta_pre_envio(
        "verifiquei no sistema e já transferi para o financeiro.",
        tool_names_in_session={"consultar_cliente"},
    )
    tool_names = [v[0] for v in violations]
    assert "consultar_cliente" not in tool_names
    assert "transferir_departamento" in tool_names
