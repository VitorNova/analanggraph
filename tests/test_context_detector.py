"""Testes unitários do context_detector (core/context_detector.py).

Valida detecção de contexto billing/manutenção no conversation_history,
normalização de nomes, expiração, e geração de prompt extra.
"""

from datetime import datetime, timezone, timedelta

from core.context_detector import detect_context, build_context_prompt


# ── Helpers ──

def _history_with_context(context: str, ref_id: str = "ref_123", hours_ago: int = 1):
    """Cria conversation_history com uma mensagem de contexto."""
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {
        "messages": [
            {"role": "system", "content": "Disparo automático", "context": context, "reference_id": ref_id, "timestamp": ts},
            {"role": "user", "content": "Oi, recebi uma mensagem"},
        ]
    }


# ── detect_context ──

def test_detecta_billing():
    ctx_type, ref_id = detect_context(_history_with_context("billing"))
    assert ctx_type == "billing"
    assert ref_id == "ref_123"


def test_detecta_disparo_billing():
    ctx_type, _ = detect_context(_history_with_context("disparo_billing"))
    assert ctx_type == "billing"


def test_detecta_disparo_cobranca():
    ctx_type, _ = detect_context(_history_with_context("disparo_cobranca"))
    assert ctx_type == "billing"


def test_detecta_manutencao():
    ctx_type, _ = detect_context(_history_with_context("manutencao_preventiva"))
    assert ctx_type == "manutencao"


def test_detecta_disparo_manutencao():
    ctx_type, _ = detect_context(_history_with_context("disparo_manutencao"))
    assert ctx_type == "manutencao"


def test_sem_contexto():
    history = {"messages": [{"role": "user", "content": "Oi"}]}
    ctx_type, ref_id = detect_context(history)
    assert ctx_type is None
    assert ref_id is None


def test_historico_vazio():
    ctx_type, ref_id = detect_context({})
    assert ctx_type is None
    assert ref_id is None


def test_historico_none():
    ctx_type, ref_id = detect_context(None)
    assert ctx_type is None
    assert ref_id is None


def test_contexto_expirado():
    """Contexto > 7 dias deve ser ignorado."""
    ctx_type, _ = detect_context(_history_with_context("billing", hours_ago=200))
    assert ctx_type is None


def test_contexto_no_limite():
    """Contexto com exatamente 167h (< 168h) deve ser detectado."""
    ctx_type, _ = detect_context(_history_with_context("billing", hours_ago=167))
    assert ctx_type == "billing"


def test_reference_id_contract():
    """Deve aceitar contract_id como fallback de reference_id."""
    ts = datetime.now(timezone.utc).isoformat()
    history = {
        "messages": [
            {"role": "system", "context": "manutencao", "contract_id": "ct_456", "timestamp": ts},
        ]
    }
    _, ref_id = detect_context(history)
    assert ref_id == "ct_456"


def test_reference_id_payment():
    """Deve aceitar payment_id como fallback."""
    ts = datetime.now(timezone.utc).isoformat()
    history = {
        "messages": [
            {"role": "system", "context": "billing", "payment_id": "pay_789", "timestamp": ts},
        ]
    }
    _, ref_id = detect_context(history)
    assert ref_id == "pay_789"


# ── build_context_prompt ──

def test_prompt_billing_contem_cobranca():
    prompt = build_context_prompt("billing", "ref_123")
    assert "COBRANÇA" in prompt
    assert "buscar_por_telefone" in prompt
    assert "ref_123" in prompt


def test_prompt_manutencao_contem_preventiva():
    prompt = build_context_prompt("manutencao", "ct_456")
    assert "MANUTENÇÃO" in prompt
    assert "ct_456" in prompt
    assert "GRATUITA" in prompt  # manutenção gratuita inclusa no contrato


def test_prompt_desconhecido_retorna_vazio():
    prompt = build_context_prompt("qualquer_outro")
    assert prompt == ""
