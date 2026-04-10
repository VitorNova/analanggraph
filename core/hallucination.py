"""
Detecção de hallucination do agente.

Verifica se a Ana afirmou ter executado uma tool (ex: "transferi", "registrei")
sem realmente tê-la chamado. Retorna lista de tools com hallucination detectada.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_HALL_CHECKS = [
    ("transferir_departamento", [r"\btransferi\b", r"\bencaminhei\b", r"\bdirecionei\b", "te passo para", "vou te transferir", "vou transferir", "transferir voc", r"\bte transfiro\b", r"\bte encaminho\b"]),
    ("registrar_compromisso", [r"\bregistrei\b", r"\banotei o compromisso\b", "compromisso registrado"]),
    ("consultar_cliente", [r"\bverifiquei\b", r"\bconsultei\b", "encontrei no sistema", r"(?<!não )(?<!nao )\blocalizei\b"]),
]


def detectar_tool_como_texto(resposta: str) -> Optional[dict]:
    """
    Detecta se o Gemini escreveu uma chamada de tool como texto em vez de usar function calling.

    Bug conhecido do Gemini 2.0 Flash: emite finish_reason STOP com nome da tool como
    content (texto) em vez de functionCall no parts[].

    Args:
        resposta: Texto da resposta da Ana

    Returns:
        Dict com tool detectada e args extraídos, ou None se limpa.
        Ex: {"tool": "transferir_departamento", "queue_id": 453, "user_id": 815}
    """
    if not resposta:
        return None

    # Padrão: nome_da_tool(param=valor, param=valor)
    match = re.search(
        r"(transferir_departamento|consultar_cliente|registrar_compromisso)"
        r"\s*\(",
        resposta,
    )
    if not match:
        return None

    tool_name = match.group(1)
    result = {"tool": tool_name}

    # Extrair args se for transferência
    if tool_name == "transferir_departamento":
        q = re.search(r"queue_id\s*=\s*(\d+)", resposta)
        u = re.search(r"user_id\s*=\s*(\d+)", resposta)
        if q:
            result["queue_id"] = int(q.group(1))
        if u:
            result["user_id"] = int(u.group(1))

    logger.warning(f"[HALLUCINATION:{tool_name}] Tool escrita como texto: {resposta[:100]}")
    return result


def detectar_hallucination(novas_mensagens: list, phone: str) -> list[str]:
    """
    Detecta tools que Ana disse ter chamado mas não chamou.

    Args:
        novas_mensagens: Mensagens novas do resultado do graph (AIMessage + ToolMessage)
        phone: Telefone do lead (para logging)

    Returns:
        Lista de nomes de tools com hallucination detectada (vazia se nenhuma)
    """
    from langchain_core.messages import AIMessage

    # Extrair resposta final (último AIMessage com conteúdo)
    resposta = None
    for msg in reversed(novas_mensagens):
        if isinstance(msg, AIMessage) and msg.content:
            content = msg.content
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            if content.strip():
                resposta = content.strip()
                break

    if not resposta:
        return []

    tools_chamadas = {
        tc["name"]
        for m in novas_mensagens
        if isinstance(m, AIMessage) and m.tool_calls
        for tc in m.tool_calls
    }

    resp_lower = resposta.lower()
    hallucinations = []

    for tool_name, frases in _HALL_CHECKS:
        if tool_name not in tools_chamadas and any(re.search(f, resp_lower) for f in frases):
            hallucinations.append(tool_name)

    return hallucinations
