#!/usr/bin/env python3
"""Roda cenários de teste contra o grafo real e gera relatório detalhado estilo n8n (input/output por node)."""

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

SCENARIOS = [
    {
        "nome": "ar_quebrou",
        "mensagens": ["meu ar quebrou"],
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "atendimento"},
        "expect_not_contains": ["CPF"],
    },
    {
        "nome": "devolucao",
        "mensagens": ["quero devolver o ar"],
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "atendimento"},
        "expect_not_contains": ["CPF"],
    },
    {
        "nome": "restricao_cpf",
        "mensagens": ["quero alugar um ar mas tenho restrição no nome"],
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "financeiro"},
    },
    {
        "nome": "contestacao_fatura",
        "mensagens": ["esse valor tá errado"],
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "cobrancas"},
    },
    {
        "nome": "falar_com_dono",
        "mensagens": ["quero falar com o Lázaro"],
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "lazaro"},
    },
    {
        "nome": "pede_boleto",
        "mensagens": ["quero o boleto"],
        "expect_no_tool": True,
        "expect_contains": ["CPF"],
    },
    {
        "nome": "disparo_cobranca",
        "mensagens": ["oi"],
        "context": "billing",
        "historico": [
            {"role": "model", "content": "Oi! Aqui é da Aluga Ar. Passando pra lembrar da sua fatura de R$ 189,00 vencida dia 05/04. Pra facilitar, segue o link do boleto/pix: https://asaas.com/abc123"},
        ],
        "expect_no_tool": True,
        "expect_contains_any": ["pagamento", "pagar", "fatura", "boleto", "ajuda", "link"],
    },
    {
        "nome": "ja_paguei",
        "mensagens": ["já paguei essa"],
        "context": "billing",
        "historico": [
            {"role": "user", "content": "quero ver minha fatura"},
            {"role": "model", "content": "Me passa seu CPF?"},
            {"role": "user", "content": "12345678900"},
            {"role": "model", "content": "Você tem 1 fatura pendente de R$ 189"},
        ],
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "financeiro"},
    },
    {
        "nome": "promete_pagar_sexta",
        "mensagens": ["vou pagar sexta"],
        "context": "billing",
        "expect_tool": "registrar_compromisso",
    },
    {
        "nome": "promessa_vaga",
        "mensagens": ["vou tentar pagar"],
        "context": "billing",
        "expect_no_tool": True,
    },
    {
        "nome": "pergunta_preco",
        "mensagens": ["quanto custa alugar um ar?"],
        "expect_no_tool": True,
        "expect_contains": ["m²"],
    },
    {
        "nome": "informa_metragem",
        "mensagens": ["quarto de 12m²"],
        "historico": [
            {"role": "user", "content": "quero alugar um ar"},
            {"role": "model", "content": "Pra qual ambiente e quantos m²?"},
        ],
        "expect_no_tool": True,
        "expect_contains": ["189"],
    },
    {
        "nome": "coleta_dados_novo_aluguel",
        "mensagens": ["João Silva, 12345678900"],
        "historico": [
            {"role": "user", "content": "quero alugar um ar pro quarto"},
            {"role": "model", "content": "Pra 12m² fica R$ 189/mês. Quer seguir?"},
            {"role": "user", "content": "quero"},
            {"role": "model", "content": "Me passa seu nome e CPF?"},
        ],
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "atendimento"},
    },
    {
        "nome": "ar_pingando",
        "mensagens": ["o ar tá pingando água"],
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "atendimento"},
        "expect_not_contains": ["CPF"],
    },
    {
        "nome": "fora_area",
        "mensagens": ["vocês atendem em Cuiabá?"],
        "expect_tool": "transferir_departamento",
        "expect_tool_args": {"destino": "atendimento"},
    },
]


async def run_cenario(cenario: dict) -> dict:
    """Executa um cenário e retorna fluxo completo estilo n8n."""
    nome = cenario["nome"]
    test_phone = cenario.get("phone", "5500000000000")

    # Preparar resultado
    result = {
        "nome": nome,
        "descricao": cenario.get("descricao", ""),
        "context": cenario.get("context"),
        "flow": [],  # lista de nodes estilo n8n
        "validations": [],
        "passed": True,
        "duration_ms": 0,
    }

    # NODE 1: Input — mensagens do cliente
    lang_msgs = []
    historico_input = []
    for h in cenario.get("historico", []):
        role = h.get("role", "user")
        historico_input.append(h)
        if role == "model":
            lang_msgs.append(AIMessage(content=h["content"]))
        else:
            lang_msgs.append(HumanMessage(content=h["content"]))
    lang_msgs.extend([HumanMessage(content=m) for m in cenario["mensagens"]])

    result["flow"].append({
        "node": "1_INPUT",
        "type": "client_messages",
        "data": {
            "historico": historico_input if historico_input else None,
            "mensagens": cenario["mensagens"],
            "phone": test_phone,
            "context_injected": cenario.get("context"),
        },
    })

    # Injetar contexto se definido
    if cenario.get("context"):
        try:
            from core.context_detector import build_context_prompt
            ctx_prompt = build_context_prompt(cenario["context"])
            _context_extra[test_phone] = {"type": cenario["context"], "prompt": ctx_prompt}
            result["flow"].append({
                "node": "2_CONTEXT_INJECTION",
                "type": "context",
                "data": {
                    "context_type": cenario["context"],
                    "prompt_snippet": ctx_prompt[:200] + "..." if len(ctx_prompt) > 200 else ctx_prompt,
                },
            })
        except Exception as e:
            result["flow"].append({
                "node": "2_CONTEXT_INJECTION",
                "type": "error",
                "data": {"error": str(e)},
            })

    # NODE 3: Invocar grafo
    try:
        t0 = time.time()
        state = await graph.ainvoke({
            "messages": lang_msgs,
            "phone": test_phone,
        })
        elapsed = int((time.time() - t0) * 1000)
        result["duration_ms"] = elapsed
    except Exception as e:
        result["passed"] = False
        result["flow"].append({
            "node": "3_GRAPH_INVOKE",
            "type": "error",
            "data": {"error": str(e), "error_type": type(e).__name__},
        })
        result["validations"].append({"check": "graph_invoke", "passed": False, "detail": str(e)})
        return result
    finally:
        _context_extra.pop(test_phone, None)

    result["flow"].append({
        "node": "3_GRAPH_INVOKE",
        "type": "graph",
        "data": {
            "duration_ms": elapsed,
            "total_messages_returned": len(state.get("messages", [])),
        },
    })

    # Parsear mensagens retornadas — cada uma vira um node
    tools_called = []
    tool_results_list = []
    response_text = ""
    node_counter = 4

    for msg in state.get("messages", []):
        if isinstance(msg, HumanMessage):
            # Pular mensagens de input (já registradas)
            continue

        if isinstance(msg, AIMessage):
            # Tool calls
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tools_called.append({"name": tc["name"], "args": tc["args"]})
                    result["flow"].append({
                        "node": f"{node_counter}_TOOL_CALL",
                        "type": "tool_call",
                        "data": {
                            "tool": tc["name"],
                            "args": tc["args"],
                        },
                    })
                    node_counter += 1

            # Conteúdo texto
            if msg.content:
                content = msg.content
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
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
                "data": {
                    "tool": tool_name,
                    "result": tool_content,
                },
            })
            node_counter += 1

    # INTERCEPTOR: mesmo que processar_mensagens() em produção
    # Se Gemini escreveu tool como texto, detectar e contar como tool chamada
    from core.hallucination import detectar_tool_como_texto
    tool_texto = detectar_tool_como_texto(response_text) if response_text else None
    if tool_texto:
        result["flow"].append({
            "node": f"{node_counter}_INTERCEPTOR",
            "type": "interceptor",
            "data": {
                "detected": tool_texto,
                "original_text": response_text,
                "action": "tool executada via interceptor (produção executaria a tool real)",
            },
        })
        node_counter += 1
        # Adicionar como tool chamada (em produção o interceptor executa)
        intercepted_args = {}
        if tool_texto.get("destino"):
            intercepted_args["destino"] = tool_texto["destino"]
        tools_called.append({"name": tool_texto["tool"], "args": intercepted_args, "via": "interceptor"})
        # Limpar response_text (em produção não chega ao cliente)
        response_text = ""

    # NODE FINAL: Output consolidado
    result["flow"].append({
        "node": f"{node_counter}_OUTPUT",
        "type": "final_output",
        "data": {
            "response_text": response_text,
            "tools_called": tools_called,
            "tool_results": tool_results_list,
        },
    })

    # =========================================================================
    # VALIDAÇÕES
    # =========================================================================

    # expect_tool
    if "expect_tool" in cenario:
        expected = cenario["expect_tool"]
        called_names = [t["name"] for t in tools_called]
        ok = expected in called_names
        result["validations"].append({
            "check": "expect_tool",
            "expected": expected,
            "actual": called_names,
            "passed": ok,
        })
        if not ok:
            result["passed"] = False

    # expect_tool_args
    if "expect_tool_args" in cenario and "expect_tool" in cenario:
        expected_tool = cenario["expect_tool"]
        expected_args = cenario["expect_tool_args"]
        matched = False
        actual_args = {}
        for tc in tools_called:
            if tc["name"] == expected_tool:
                actual_args = tc["args"]
                all_match = all(
                    tc["args"].get(k) == v for k, v in expected_args.items()
                )
                if all_match:
                    matched = True
                break
        result["validations"].append({
            "check": "expect_tool_args",
            "expected": expected_args,
            "actual": actual_args,
            "passed": matched,
        })
        if not matched:
            result["passed"] = False

    # expect_no_tool
    if cenario.get("expect_no_tool"):
        called_names = [t["name"] for t in tools_called]
        ok = len(called_names) == 0
        result["validations"].append({
            "check": "expect_no_tool",
            "expected": "no tools",
            "actual": called_names if called_names else "none",
            "passed": ok,
        })
        if not ok:
            result["passed"] = False

    # expect_contains
    for word in cenario.get("expect_contains", []):
        ok = word.lower() in response_text.lower()
        result["validations"].append({
            "check": "expect_contains",
            "expected": word,
            "found": ok,
            "passed": ok,
        })
        if not ok:
            result["passed"] = False

    # expect_contains_any (OR — pelo menos uma palavra presente)
    if "expect_contains_any" in cenario:
        words = cenario["expect_contains_any"]
        found_any = any(w.lower() in response_text.lower() for w in words)
        result["validations"].append({
            "check": "expect_contains_any",
            "expected_any_of": words,
            "found": found_any,
            "passed": found_any,
        })
        if not found_any:
            result["passed"] = False

    # expect_not_contains
    for word in cenario.get("expect_not_contains", []):
        found = word.lower() in response_text.lower()
        ok = not found
        result["validations"].append({
            "check": "expect_not_contains",
            "expected_absent": word,
            "found": found,
            "passed": ok,
        })
        if not ok:
            result["passed"] = False

    return result


async def main():
    print(f"\n{'='*70}")
    print(f"  TESTE DE CENÁRIOS — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
                    print(f"       ✗ {f['check']}: esperado={f.get('expected', f.get('expected_absent', '?'))}, "
                          f"real={f.get('actual', f.get('found', '?'))}")
        except Exception as e:
            failed += 1
            results.append({
                "nome": nome,
                "passed": False,
                "flow": [],
                "validations": [{"check": "exception", "passed": False, "detail": str(e)}],
                "duration_ms": 0,
            })
            print(f"ERROR: {e}")

    # Relatório final
    report = {
        "timestamp": datetime.now().isoformat(),
        "total": len(SCENARIOS),
        "passed": passed,
        "failed": failed,
        "scenarios": results,
    }

    # Gerar nome único com timestamp
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = f"/var/www/ana-langgraph/tests/scenario_results_{ts}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*70}")
    print(f"  RESULTADO: {passed}/{len(SCENARIOS)} PASS | {failed} FAIL")
    print(f"  Relatório salvo em: {output_path}")
    print(f"{'='*70}\n")

    return report


if __name__ == "__main__":
    asyncio.run(main())
