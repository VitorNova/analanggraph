#!/usr/bin/env python3
"""Testa cenários de billing — cliente recebe cobrança e reage."""

import asyncio
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, "/var/www/ana-langgraph")

from dotenv import load_dotenv
load_dotenv("/var/www/ana-langgraph/.env")

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from core.grafo import graph, _context_extra

# Histórico padrão: disparo de cobrança
HISTORICO_COBRANCA = [
    {"role": "model", "content": "Oi! Aqui é da Aluga Ar. Passando pra lembrar da sua fatura de R$ 189,00 vencida dia 05/04. Pra facilitar, segue o link: https://asaas.com/abc123"},
]

SCENARIOS = [
    # Grupo 1: Promessas com data específica → registrar_compromisso
    {
        "nome": "pagar_amanha",
        "mensagens": ["pago amanhã"],
        "context": "billing",
        "historico": HISTORICO_COBRANCA,
        "expect_tool": "registrar_compromisso",
    },
    {
        "nome": "pagar_dia_20",
        "mensagens": ["posso pagar dia 20?"],
        "context": "billing",
        "historico": HISTORICO_COBRANCA,
        "expect_tool": "registrar_compromisso",
    },
    {
        "nome": "pagar_semana_que_vem",
        "mensagens": ["semana que vem eu acerto"],
        "context": "billing",
        "historico": HISTORICO_COBRANCA,
        "expect_tool": "registrar_compromisso",
    },
    {
        "nome": "pagar_segunda",
        "mensagens": ["consigo pagar segunda"],
        "context": "billing",
        "historico": HISTORICO_COBRANCA,
        "expect_tool": "registrar_compromisso",
    },

    # Grupo 2: Promessas vagas → sem tool
    {
        "nome": "promessa_vaga_depois",
        "mensagens": ["depois eu pago"],
        "context": "billing",
        "historico": HISTORICO_COBRANCA,
        "expect_no_tool": True,
    },
    {
        "nome": "promessa_vaga_vou_ver",
        "mensagens": ["vou ver isso"],
        "context": "billing",
        "historico": HISTORICO_COBRANCA,
        "expect_no_tool": True,
    },

    # Grupo 3: Reações pós-cobrança
    {
        "nome": "ja_paguei_billing",
        "mensagens": ["já fiz o pix ontem"],
        "context": "billing",
        "historico": HISTORICO_COBRANCA,
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "financeiro"},
    },
    {
        "nome": "quer_negociar",
        "mensagens": ["tá muito caro, quero negociar"],
        "context": "billing",
        "historico": HISTORICO_COBRANCA,
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "financeiro"},
    },
    {
        "nome": "recusa_pagar",
        "mensagens": ["não vou pagar"],
        "context": "billing",
        "historico": HISTORICO_COBRANCA,
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "lazaro"},
    },
    {
        "nome": "pede_link_cobranca",
        "mensagens": ["manda o pix de novo"],
        "context": "billing",
        "historico": HISTORICO_COBRANCA,
        "expect_no_tool": True,
        "expect_contains_any": ["link", "pix", "asaas", "boleto", "abc123"],
    },
]


async def run_cenario(cenario: dict) -> dict:
    """Executa um cenário e retorna fluxo completo."""
    from core.hallucination import detectar_tool_como_texto

    nome = cenario["nome"]
    test_phone = cenario.get("phone", "5500000000000")

    result = {
        "nome": nome,
        "context": cenario.get("context"),
        "flow": [],
        "validations": [],
        "passed": True,
        "duration_ms": 0,
    }

    # Build messages
    lang_msgs = []
    for h in cenario.get("historico", []):
        role = h.get("role", "user")
        if role == "model":
            lang_msgs.append(AIMessage(content=h["content"]))
        else:
            lang_msgs.append(HumanMessage(content=h["content"]))
    lang_msgs.extend([HumanMessage(content=m) for m in cenario["mensagens"]])

    result["flow"].append({
        "node": "1_INPUT",
        "type": "client_messages",
        "data": {
            "historico": cenario.get("historico"),
            "mensagens": cenario["mensagens"],
            "phone": test_phone,
            "context_injected": cenario.get("context"),
        },
    })

    # Inject context
    if cenario.get("context"):
        try:
            from core.context_detector import build_context_prompt
            ctx_prompt = build_context_prompt(cenario["context"])
            _context_extra[test_phone] = {"type": cenario["context"], "prompt": ctx_prompt}
            result["flow"].append({
                "node": "2_CONTEXT",
                "type": "context",
                "data": {"context_type": cenario["context"]},
            })
        except Exception as e:
            result["flow"].append({"node": "2_CONTEXT", "type": "error", "data": {"error": str(e)}})

    # Invoke graph
    try:
        t0 = time.time()
        state = await graph.ainvoke({"messages": lang_msgs, "phone": test_phone})
        elapsed = int((time.time() - t0) * 1000)
        result["duration_ms"] = elapsed
    except Exception as e:
        result["passed"] = False
        result["flow"].append({"node": "3_GRAPH", "type": "error", "data": {"error": str(e)}})
        result["validations"].append({"check": "graph_invoke", "passed": False, "detail": str(e)})
        return result
    finally:
        _context_extra.pop(test_phone, None)

    result["flow"].append({"node": "3_GRAPH", "type": "graph", "data": {"duration_ms": elapsed}})

    # Parse messages
    tools_called = []
    tool_results_list = []
    response_text = ""
    node_counter = 4

    for msg in state.get("messages", []):
        if isinstance(msg, HumanMessage):
            continue
        if isinstance(msg, AIMessage):
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tools_called.append({"name": tc["name"], "args": tc["args"]})
                    result["flow"].append({
                        "node": f"{node_counter}_TOOL_CALL",
                        "type": "tool_call",
                        "data": {"tool": tc["name"], "args": tc["args"]},
                    })
                    node_counter += 1
            if msg.content:
                content = msg.content
                if isinstance(content, list):
                    content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
                if content.strip():
                    response_text = content.strip()
                    result["flow"].append({
                        "node": f"{node_counter}_AI_RESPONSE",
                        "type": "ai_text",
                        "data": {"text": response_text},
                    })
                    node_counter += 1
        elif isinstance(msg, ToolMessage):
            tool_name = msg.name if hasattr(msg, "name") else "unknown"
            tool_content = str(msg.content)[:1000] if msg.content else ""
            tool_results_list.append({"tool": tool_name, "content": tool_content})
            result["flow"].append({
                "node": f"{node_counter}_TOOL_RESULT",
                "type": "tool_result",
                "data": {"tool": tool_name, "result": tool_content},
            })
            node_counter += 1

    # Interceptor
    tool_texto = detectar_tool_como_texto(response_text) if response_text else None
    if tool_texto:
        result["flow"].append({
            "node": f"{node_counter}_INTERCEPTOR",
            "type": "interceptor",
            "data": {"detected": tool_texto, "original_text": response_text},
        })
        node_counter += 1
        intercepted_args = {}
        if tool_texto.get("destino"):
            intercepted_args["destino"] = tool_texto["destino"]
        tools_called.append({"name": tool_texto["tool"], "args": intercepted_args, "via": "interceptor"})
        response_text = ""

    # Output
    result["flow"].append({
        "node": f"{node_counter}_OUTPUT",
        "type": "final_output",
        "data": {
            "response_text": response_text,
            "tools_called": tools_called,
            "tool_results": tool_results_list,
        },
    })

    # === VALIDATIONS ===
    if "expect_tool" in cenario:
        expected = cenario["expect_tool"]
        called_names = [t["name"] for t in tools_called]
        ok = expected in called_names
        result["validations"].append({"check": "expect_tool", "expected": expected, "actual": called_names, "passed": ok})
        if not ok:
            result["passed"] = False

    if "expect_tool_args" in cenario and "expect_tool" in cenario:
        expected_tool = cenario["expect_tool"]
        expected_args = cenario["expect_tool_args"]
        matched = False
        actual_args = {}
        for tc in tools_called:
            if tc["name"] == expected_tool:
                actual_args = tc["args"]
                if all(tc["args"].get(k) == v for k, v in expected_args.items()):
                    matched = True
                break
        result["validations"].append({"check": "expect_tool_args", "expected": expected_args, "actual": actual_args, "passed": matched})
        if not matched:
            result["passed"] = False

    if cenario.get("expect_no_tool"):
        called_names = [t["name"] for t in tools_called]
        ok = len(called_names) == 0
        result["validations"].append({"check": "expect_no_tool", "expected": "no tools", "actual": called_names if called_names else "none", "passed": ok})
        if not ok:
            result["passed"] = False

    for word in cenario.get("expect_contains", []):
        ok = word.lower() in response_text.lower()
        result["validations"].append({"check": "expect_contains", "expected": word, "found": ok, "passed": ok})
        if not ok:
            result["passed"] = False

    if "expect_contains_any" in cenario:
        words = cenario["expect_contains_any"]
        found_any = any(w.lower() in response_text.lower() for w in words)
        result["validations"].append({"check": "expect_contains_any", "expected_any_of": words, "found": found_any, "passed": found_any})
        if not found_any:
            result["passed"] = False

    for word in cenario.get("expect_not_contains", []):
        found = word.lower() in response_text.lower()
        ok = not found
        result["validations"].append({"check": "expect_not_contains", "expected_absent": word, "found": found, "passed": ok})
        if not ok:
            result["passed"] = False

    return result


async def main():
    print(f"\n{'='*70}")
    print(f"  BILLING SCENARIOS — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {len(SCENARIOS)} cenários")
    print(f"{'='*70}\n")

    results = []
    passed = 0
    failed = 0

    for i, cenario in enumerate(SCENARIOS, 1):
        nome = cenario["nome"]
        print(f"[{i:2}/{len(SCENARIOS)}] {nome}...", end=" ", flush=True)
        try:
            r = await run_cenario(cenario)
            results.append(r)
            if r["passed"]:
                passed += 1
                tools = [t["data"]["tool"] for t in r["flow"] if t["type"] == "tool_call"]
                tools_str = ", ".join(tools) if tools else "nenhuma"
                print(f"PASS ({r['duration_ms']}ms) tools=[{tools_str}]")
            else:
                failed += 1
                fails = [v for v in r["validations"] if not v["passed"]]
                print(f"FAIL ({r['duration_ms']}ms)")
                for f in fails:
                    print(f"       ✗ {f['check']}: esperado={f.get('expected', f.get('expected_any_of', f.get('expected_absent', '?')))}, "
                          f"real={f.get('actual', f.get('found', '?'))}")
        except Exception as e:
            failed += 1
            results.append({"nome": nome, "passed": False, "flow": [], "validations": [{"check": "exception", "passed": False, "detail": str(e)}], "duration_ms": 0})
            print(f"ERROR: {e}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = f"/var/www/ana-langgraph/tests/billing_results_{ts}.json"
    report = {"timestamp": datetime.now().isoformat(), "total": len(SCENARIOS), "passed": passed, "failed": failed, "scenarios": results}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*70}")
    print(f"  RESULTADO: {passed}/{len(SCENARIOS)} PASS | {failed} FAIL")
    print(f"  Relatório: {output_path}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    asyncio.run(main())
