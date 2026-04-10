#!/usr/bin/env python3
"""Reproduz a conversa real do lead 556699198912 que causou o bug tool-as-text.

Injeta o histórico completo como state["messages"] e manda o último input.
Salva resultado como flow JSON em tests/flows/bug_original_limpeza_ar.json.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("bug_original")


def build_historico():
    """Reconstrói o histórico real da conversa problemática."""
    return [
        HumanMessage(content="Olá bom dia tudo bom"),
        AIMessage(content="Olá, bom dia! Tudo bem por aqui. Em que posso te ajudar hoje?"),
        HumanMessage(content="Tenho um ar condicionado em casa preciso fazer uma limpeza ar de 12 mil btus inverter , casa bairro São Sebastião II\nQual dia estarão disponível??\nBom dia"),
        # Na conversa real, aqui a Ana escreveu "transferir_departamento(queue_id=453, user_id=815)" como texto
        # Simulamos como o histórico ficou salvo: role model com texto literal
        AIMessage(content="transferir_departamento(queue_id=453, user_id=815)"),
        HumanMessage(content="Bom dia"),
        AIMessage(content="Olá! Em que posso te ajudar hoje? 😊"),
        HumanMessage(content="Tenho um ar condicionado em casa preciso fazer uma limpeza ar de 12 mil btus inverter , casa bairro São Sebastião II\nBom dia"),
        AIMessage(content="transferir_departamento(queue_id=453, user_id=815)"),
        HumanMessage(content="Está bem"),
        AIMessage(content="Em que posso te ajudar? 😊"),
        HumanMessage(content="Tenho um ar condicionado em casa preciso fazer uma limpeza ar de 12 mil btus inverter , casa bairro São Sebastião II"),
        AIMessage(content="transferir_departamento(queue_id=453, user_id=815)"),
        HumanMessage(content="Qual dia estão disponíveis para busca e fazer a limpeza"),
        AIMessage(content="transferir_departamento(queue_id=453, user_id=815)"),
        HumanMessage(content="Bom dia Ana"),
        AIMessage(content="Olá! Bom dia! Em que posso te ajudar hoje? 😊"),
    ]


async def run():
    from core.grafo import graph, GEMINI_MODEL
    from core.prompts import SYSTEM_PROMPT
    from infra.nodes_supabase import buscar_historico

    # Buscar histórico real do lead via buscar_historico() — aplica sanitização
    historico_real = buscar_historico("556699198912", limite=20)
    # Fallback: se lead não existe no banco, usa histórico manual
    if not historico_real:
        print("Lead não encontrado no Supabase, usando histórico manual")
        historico_real = build_historico()
    else:
        print(f"Histórico carregado do Supabase: {len(historico_real)} mensagens")
        # Mostrar se sanitização agiu
        for i, msg in enumerate(historico_real):
            if isinstance(msg, AIMessage) and not msg.content and not msg.tool_calls:
                print(f"  msg[{i}]: AIMessage content='' (sanitizado)")

    historico = historico_real
    ultimo_input = "Qual dia vocês pode está indo busca?"
    messages = historico + [HumanMessage(content=ultimo_input)]

    print(f"Modelo: {GEMINI_MODEL}")
    print(f"Histórico: {len(historico)} mensagens")
    print(f"Último input: {ultimo_input}")
    print(f"Rodando graph.ainvoke()...")

    t0 = time.time()
    result = await graph.ainvoke({
        "messages": messages,
        "phone": "556699198912",
    })
    duracao_ms = int((time.time() - t0) * 1000)

    # Extrair mensagens novas (após o histórico injetado)
    novas = result["messages"][len(messages):]

    # Serializar AIMessages brutos
    raw_messages = []
    for msg in novas:
        if isinstance(msg, AIMessage):
            raw = {
                "type": "ai",
                "content": msg.content[:500] if isinstance(msg.content, str) else str(msg.content)[:500],
                "tool_calls": [{"name": tc["name"], "args": tc.get("args", {})} for tc in (msg.tool_calls or [])],
            }
            if hasattr(msg, "response_metadata") and msg.response_metadata:
                raw["finish_reason"] = msg.response_metadata.get("finish_reason", "")
            raw_messages.append(raw)
        elif isinstance(msg, ToolMessage):
            raw_messages.append({
                "type": "tool",
                "name": msg.name if hasattr(msg, "name") else "",
                "content": str(msg.content)[:300],
            })

    # Extrair resposta final
    resposta = ""
    for msg in reversed(novas):
        if isinstance(msg, AIMessage) and msg.content:
            content = msg.content
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            if content.strip():
                resposta = content.strip()
                break

    # Extrair tool calls
    tool_calls = []
    for msg in novas:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({"name": tc["name"], "args": tc.get("args", {})})

    # Validações
    from core.hallucination import detectar_tool_como_texto
    tool_texto = detectar_tool_como_texto(resposta) if resposta else None
    tool_chamada = len(tool_calls) > 0
    content_limpo = not tool_texto

    # Determinar resultado
    # Bug original: Ana escrevia "transferir_departamento(queue_id=453, user_id=815)" como texto
    # PASS = tool chamada via function calling OU resposta sem sintaxe de tool
    passou = content_limpo and (tool_chamada or not any(
        nome in resposta for nome in ["transferir_departamento", "consultar_cliente", "registrar_compromisso"]
    ))

    # Montar flow JSON
    flow = {
        "flow_name": "bug_original_limpeza_ar",
        "data": "2026-04-10",
        "nos": [
            {
                "id": 1,
                "tipo": "INPUT",
                "label": "Histórico injetado",
                "dados": {
                    "total_mensagens": len(messages),
                    "historico_mensagens": len(historico),
                    "ultimo_input": ultimo_input,
                    "lead_original": "556699198912",
                }
            },
            {
                "id": 2,
                "tipo": "GRAFO",
                "label": "Invocação graph.ainvoke()",
                "dados": {
                    "modelo": GEMINI_MODEL,
                    "prompt_version": "pos-correcao",
                    "nota": "Prompt sem sintaxe literal de tool calls (Ação 3 aplicada)"
                }
            },
            {
                "id": 3,
                "tipo": "OUTPUT_IA",
                "label": "O que o modelo retornou",
                "dados": {
                    "raw_messages": raw_messages,
                    "content": resposta[:500],
                    "tool_calls": tool_calls,
                }
            },
            {
                "id": 4,
                "tipo": "VALIDACAO",
                "label": "Checagens",
                "dados": {
                    "tool_como_texto": bool(tool_texto),
                    "tool_chamada": tool_chamada,
                    "content_limpo": content_limpo,
                    "tool_texto_detalhe": tool_texto if tool_texto else None,
                }
            },
            {
                "id": 5,
                "tipo": "RESULTADO",
                "label": "PASS" if passou else "FAIL",
                "dados": {
                    "resultado": "PASS" if passou else "FAIL",
                    "tempo_ms": duracao_ms,
                    "observacao": (
                        "Tool chamada via function calling, content limpo"
                        if tool_chamada and content_limpo
                        else "Content limpo, sem tool-as-text"
                        if content_limpo
                        else "BUG REPRODUZIDO: tool escrita como texto no content"
                    )
                }
            }
        ]
    }

    # Salvar
    out_path = PROJECT_ROOT / "tests" / "flows" / "bug_original_limpeza_ar.json"
    out_path.write_text(json.dumps(flow, indent=2, ensure_ascii=False))
    print(f"\nSalvo: {out_path}")

    # Imprimir resultado
    print(f"\nResultado: {'PASS' if passou else 'FAIL'} ({duracao_ms}ms)")
    print(f"Tool calls: {tool_calls}")
    print(f"Content: {resposta[:200]}")
    print(f"Tool como texto: {bool(tool_texto)}")

    return passou


if __name__ == "__main__":
    result = asyncio.run(run())
    sys.exit(0 if result else 1)
