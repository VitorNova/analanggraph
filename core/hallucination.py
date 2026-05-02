"""
Detecção de hallucination do agente.

Verifica se a Ana afirmou ter executado uma tool (ex: "transferi", "registrei")
sem realmente tê-la chamado. Retorna lista de tools com hallucination detectada.
"""

import logging
import re
from typing import Optional

from core.constants import QUEUE_ATENDIMENTO, QUEUE_FINANCEIRO, QUEUE_BILLING

logger = logging.getLogger(__name__)

_HALL_CHECKS = [
    ("transferir_departamento", [r"\btransferi\b", r"\bencaminhei\b", r"\bdirecionei\b", "te passo para", "transferir voc"]),
    ("registrar_compromisso", [r"\bregistrei\b", r"\banotei o compromisso\b", "compromisso registrado"]),
    ("consultar_cliente", [r"\bverifiquei\b", r"\bconsultei\b", "encontrei no sistema", r"(?<!não )(?<!nao )\blocalizei\b"]),
]

_SETOR_TO_DESTINO = {
    "atendimento": "atendimento", "nathália": "atendimento", "nathalia": "atendimento",
    "financeiro": "financeiro", "tieli": "financeiro",
    "cobranças": "cobrancas", "cobrancas": "cobrancas",
    "lázaro": "lazaro", "lazaro": "lazaro", "dono": "lazaro",
}


def inferir_destino_do_texto(resposta: str) -> Optional[str]:
    """Tenta extrair destino de transferência a partir do texto natural da Ana.

    Usado como contingência quando hallucination de transferir_departamento é detectada
    mas não há tool_call com destino explícito.

    Returns:
        Nome do destino ("atendimento", "financeiro", "cobrancas", "lazaro") ou None.
    """
    if not resposta:
        return None
    resp_lower = resposta.lower()
    for setor, destino in _SETOR_TO_DESTINO.items():
        if setor in resp_lower:
            return destino
    # "para lá" sem setor explícito → fallback atendimento (caso mais comum)
    if "transferir" in resp_lower or "encaminh" in resp_lower:
        return "atendimento"
    return None


def detectar_tool_como_texto(resposta: str) -> Optional[dict]:
    """
    Detecta se o Gemini escreveu uma chamada de tool como texto em vez de usar function calling.

    Bug conhecido do Gemini 2.0 Flash: emite finish_reason STOP com nome da tool como
    content (texto) em vez de functionCall no parts[].

    Args:
        resposta: Texto da resposta da Ana

    Returns:
        Dict com tool detectada e args extraídos, ou None se limpa.
        Ex: {"tool": "transferir_departamento", "destino": "atendimento"}
    """
    if not resposta:
        return None

    # Destinos válidos para mapeamento
    _DESTINOS_VALIDOS = {"atendimento", "financeiro", "cobrancas", "lazaro"}
    _QUEUE_TO_DESTINO = {str(QUEUE_ATENDIMENTO): "atendimento", str(QUEUE_FINANCEIRO): "financeiro", str(QUEUE_BILLING): "cobrancas"}

    # === DETECÇÃO 1: formato função — tool_name(args) ===
    match = re.search(
        r"(transferir_departamento|consultar_cliente|registrar_compromisso)"
        r"\s*\(",
        resposta,
    )
    if match:
        tool_name = match.group(1)
        result = {"tool": tool_name}

        if tool_name == "transferir_departamento":
            d = re.search(r'destino\s*=\s*["\'](\w+)["\']', resposta)
            if d:
                result["destino"] = d.group(1)
            else:
                q = re.search(r"queue_id\s*=\s*(\d+)", resposta)
                if q:
                    result["destino"] = _QUEUE_TO_DESTINO.get(q.group(1), "atendimento")

        logger.warning(f"[HALLUCINATION:{tool_name}] Tool como texto (formato função): {resposta[:100]}")
        return result

    # === DETECÇÃO 2: formato descritivo — "Chamar transferir_departamento com..." ===
    match2 = re.search(
        r"[Cc]hama(?:r|ndo)?(?:\s+\w+)*\s+[`]?(transferir_departamento|consultar_cliente|registrar_compromisso)[`]?",
        resposta,
    )
    if match2:
        tool_name = match2.group(1)
        result = {"tool": tool_name}

        if tool_name == "transferir_departamento":
            for dest in _DESTINOS_VALIDOS:
                if dest in resposta.lower():
                    result["destino"] = dest
                    break

        logger.warning(f"[HALLUCINATION:{tool_name}] Tool como texto (formato descritivo): {resposta[:100]}")
        return result

    # === DETECÇÃO 3: formato narrativo — "(transfere para atendimento)", "[transferindo para...]" ===
    match3 = re.search(
        r"[\[\(]\s*(?:silenciosamente\s+)?(?:transfere|transferindo|transferir)\s+(?:para\s+)?(?:o\s+)?(\w+)",
        resposta,
        re.IGNORECASE,
    )
    if match3:
        destino_raw = match3.group(1).lower()
        destino = _SETOR_TO_DESTINO.get(destino_raw)
        if destino:
            logger.warning(f"[HALLUCINATION:transferir_departamento] Tool como texto (formato narrativo): {resposta[:100]}")
            return {"tool": "transferir_departamento", "destino": destino}

    return None


_RECUSA_PATTERNS = [
    "tudo bem", "não preciso", "não quero", "tá tudo ok",
    "nao preciso", "nao quero", "ta tudo ok", "não, obrigado",
    "nao obrigado", "tudo certo", "não é necessário", "nao e necessario",
]


def checar_contexto_sem_tool(
    context_type: Optional[str],
    content: str,
    tool_names_in_session: set[str],
) -> Optional[tuple[str, str]]:
    """Checa se o contexto exige tool que o LLM não chamou.

    Diferente de checar_resposta_pre_envio (detecta hallucination de TEXTO),
    esta detecta OMISSÃO: contexto exige ação, LLM respondeu sem agir.

    Args:
        context_type: "billing", "manutencao" ou None.
        content: Texto da resposta do LLM.
        tool_names_in_session: Tools já chamadas nesta sessão.

    Returns:
        (tool_name, destino) se violação detectada, None se OK.
    """
    if not context_type or not content:
        return None

    if context_type == "manutencao":
        if "transferir_departamento" in tool_names_in_session:
            return None  # já transferiu, OK
        # Único caso que NÃO precisa transferir: cliente recusou
        content_lower = content.lower()
        if any(p in content_lower for p in _RECUSA_PATTERNS):
            return None
        return ("transferir_departamento", "atendimento")

    return None


def checar_resposta_pre_envio(content: str, tool_names_in_session: set[str]) -> list[tuple[str, str]]:
    """Checa se a resposta afirma ação sem tool call (guardrail preventivo).

    Usado DENTRO de call_model(), ANTES do return — resposta errada nunca entra no State.
    Reutiliza os mesmos patterns de _HALL_CHECKS.

    Args:
        content: Texto da resposta do LLM (já em lowercase).
        tool_names_in_session: Set de nomes de tools já chamadas nesta sessão.

    Returns:
        Lista de (tool_name, pattern_matched) para cada violação encontrada.
        Lista vazia se resposta está limpa.
    """
    if not content:
        return []
    violations = []
    content_lower = content.lower() if content != content.lower() else content
    for tool_name, patterns in _HALL_CHECKS:
        if tool_name in tool_names_in_session:
            continue
        for pat in patterns:
            if re.search(pat, content_lower):
                violations.append((tool_name, pat))
                break
    return violations
