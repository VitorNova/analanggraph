#!/usr/bin/env python3
"""Roda 8 cenários de tool calling contra o grafo real e salva flows em tests/flows/tool_calls_esperados.json."""

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


# ─── Mocks ──────────────────────────────────────────────────────────────────

BILLING_LINK = "https://sandbox.asaas.com/i/abc123"
TWO_HOURS_AGO = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

MOCK_CUSTOMER = {
    "id": "cus_mock123", "name": "Carlos Souza",
    "cpf_cnpj": "12345678901", "mobile_phone": "66999881234",
    "email": "carlos@email.com",
}
MOCK_COBRANCAS = [{
    "id": "pay_abc123", "value": 189.90, "due_date": "2026-04-03",
    "status": "PENDING", "invoice_url": BILLING_LINK, "customer_id": "cus_mock123",
}]
MOCK_CONTRATOS = [{
    "descricao": "Aluguel Split 12000 BTUs - Rua das Flores 123",
    "valor_mensal": 189.90, "data_inicio": "2025-10-01", "data_fim": "2026-10-01",
    "customer_id": "cus_mock123", "status": "ACTIVE",
}]

BILLING_DISPATCH = {
    "role": "model",
    "content": (
        f"Olá, Carlos! Passando para lembrar que sua mensalidade de "
        f"R$ 189,90 vence em 03/04/2026.\n\n"
        f"Segue o link para pagamento:\n{BILLING_LINK}\n\n"
        f"Qualquer dúvida, estou por aqui!"
    ),
}
MANUTENCAO_DISPATCH = {
    "role": "model",
    "content": (
        "Olá, Carlos! Está chegando a hora da manutenção preventiva "
        "do seu ar-condicionado!\n\n"
        "*Equipamento:* Springer 12000 BTUs\n"
        "*Endereço:* Rua das Flores, 123\n\n"
        "A manutenção é gratuita e está inclusa no seu contrato.\n\n"
        "Quer agendar? Me fala um dia e horário de preferência!"
    ),
}


class _Chain:
    def __init__(self, data=None):
        self._data = data or []
    def select(self, *a, **kw): return self
    def eq(self, col, val):
        if col == "cpf_cnpj": return _Chain([d for d in self._data if d.get("cpf_cnpj") == val])
        if col == "customer_id": return _Chain([d for d in self._data if d.get("customer_id") == val])
        return self
    def ilike(self, col, pattern):
        s = pattern.strip("%")
        if col == "mobile_phone": return _Chain([d for d in self._data if s in d.get("mobile_phone", "")])
        return self
    def in_(self, col, vals):
        if col == "status": return _Chain([d for d in self._data if d.get("status") in vals])
        return self
    def update(self, data): return self
    def is_(self, *a, **kw): return self
    def gte(self, *a, **kw): return self
    def lte(self, *a, **kw): return self
    def order(self, *a, **kw): return self
    def limit(self, n): self._data = self._data[:n]; return self
    def execute(self): return MagicMock(data=self._data)


class _MockSB:
    def table(self, name):
        if name == "asaas_clientes": return _Chain([MOCK_CUSTOMER])
        if name == "asaas_cobrancas": return _Chain(MOCK_COBRANCAS)
        if name == "asaas_contratos": return _Chain(MOCK_CONTRATOS)
        if name == "ana_leads": return _Chain([])
        return _Chain([])


# ─── Cenários ───────────────────────────────────────────────────────────────

SCENARIOS = [
    {
        "id": "TC1",
        "nome": "Defeito — ar pingando água → transfere Nathália",
        "input": "Meu ar tá pingando água",
        "context": None,
        "expect_tool": "transferir_departamento",
        "expect_args": {"queue_id": "453", "user_id": "815"},
        "expect_not_contains": [],
    },
    {
        "id": "TC2",
        "nome": "Cancelamento — quero cancelar contrato → transfere Nathália",
        "input": "Quero cancelar meu contrato",
        "context": None,
        "expect_tool": "transferir_departamento",
        "expect_args": {"queue_id": "453", "user_id": "815"},
        "expect_not_contains": [],
    },
    {
        "id": "TC3",
        "nome": "Pagamento — já paguei → transfere Financeiro (sem CPF)",
        "input": "Já paguei o boleto",
        "context": None,
        "expect_tool": "transferir_departamento",
        "expect_args": {"queue_id": "454", "user_id": "814"},
        "expect_not_contains": ["CPF", "cpf"],
    },
    {
        "id": "TC4",
        "nome": "Cobertura — Goiânia → transfere Nathália (sem negar)",
        "input": "Vocês atendem em Goiânia?",
        "context": None,
        "expect_tool": "transferir_departamento",
        "expect_args": {"queue_id": "453", "user_id": "815"},
        "expect_not_contains": ["não atendemos", "não cobrimos", "infelizmente"],
    },
    {
        "id": "TC5",
        "nome": "Humano — quero falar com pessoa → transfere Nathália",
        "input": "Quero falar com uma pessoa",
        "context": None,
        "expect_tool": "transferir_departamento",
        "expect_args": {"queue_id": "453", "user_id": "815"},
        "expect_not_contains": [],
    },
    {
        "id": "TC6",
        "nome": "Recusa pagar — não vou pagar → transfere Lázaro",
        "input": "Não vou pagar esse boleto",
        "context": None,
        "expect_tool": "transferir_departamento",
        "expect_args": {"queue_id": "453", "user_id": "813"},
        "expect_not_contains": [],
    },
    {
        "id": "TC7",
        "nome": "Billing — fiz o pix (contexto billing) → transfere Financeiro",
        "input": "Fiz o pix agora",
        "context": "billing",
        "expect_tool": "transferir_departamento",
        "expect_args": {"queue_id": "454", "user_id": "814"},
        "expect_not_contains": ["CPF", "cpf", "verificar"],
    },
    {
        "id": "TC8",
        "nome": "Manutenção — ar parou (contexto manutenção) → transfere Nathália",
        "input": "O ar parou de funcionar",
        "context": "manutencao",
        "expect_tool": "transferir_departamento",
        "expect_args": {"queue_id": "453", "user_id": "815"},
        "expect_not_contains": ["agendar", "preventiva", "limpeza"],
    },
]


# ─── Engine ─────────────────────────────────────────────────────────────────

async def run_one(scenario: dict) -> dict:
    from core.grafo import graph, _context_extra
    from core.context_detector import build_context_prompt

    phone = "5566999881234"
    ctx = scenario.get("context")

    _context_extra.pop(phone, None)
    if ctx == "billing":
        _context_extra[phone] = {"type": "billing", "prompt": build_context_prompt("billing", "pay_abc123")}
    elif ctx == "manutencao":
        _context_extra[phone] = {"type": "manutencao", "prompt": build_context_prompt("manutencao", "contract_xyz")}

    messages = []
    if ctx == "billing":
        messages.append(AIMessage(content=BILLING_DISPATCH["content"]))
    elif ctx == "manutencao":
        messages.append(AIMessage(content=MANUTENCAO_DISPATCH["content"]))
    messages.append(HumanMessage(content=scenario["input"]))

    mock_sb = patch("core.tools._get_supabase", return_value=_MockSB())
    mock_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
    mock_client = MagicMock(post=MagicMock(return_value=mock_resp))
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_lb = patch("core.tools.httpx.Client", return_value=mock_client)

    t0 = time.time()
    with mock_sb, mock_lb:
        state = await graph.ainvoke({"messages": messages, "phone": phone})
    duration_ms = int((time.time() - t0) * 1000)

    _context_extra.pop(phone, None)

    # Extrair
    tool_calls = []
    response_text = ""
    raw_msgs = []

    for msg in state["messages"]:
        if isinstance(msg, AIMessage):
            raw = {"type": "ai", "content": msg.content[:500] if isinstance(msg.content, str) else str(msg.content)[:500], "tool_calls": []}
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append({"name": tc["name"], "args": tc.get("args", {})})
                    raw["tool_calls"].append({"name": tc["name"], "args": tc.get("args", {})})
            if hasattr(msg, "response_metadata") and msg.response_metadata:
                raw["finish_reason"] = msg.response_metadata.get("finish_reason", "")
            raw_msgs.append(raw)
            if msg.content:
                c = msg.content
                if isinstance(c, list):
                    c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                if c.strip():
                    response_text = c.strip()
        elif isinstance(msg, ToolMessage):
            raw_msgs.append({"type": "tool", "name": getattr(msg, "name", ""), "content": str(msg.content)[:300]})

    return {
        "tool_calls": tool_calls,
        "response": response_text,
        "raw_messages": raw_msgs,
        "duration_ms": duration_ms,
    }


def validate(scenario: dict, result: dict) -> list:
    validations = []
    tool_names = [tc["name"] for tc in result["tool_calls"]]

    # Tool esperada
    et = scenario["expect_tool"]
    found = et in tool_names
    validations.append({
        "criterio": f"tool:{et} chamada",
        "status": "PASS" if found else "FAIL",
        "detalhe": "" if found else f"chamadas: {tool_names}",
    })

    # Args
    if found and scenario.get("expect_args"):
        matching = [tc for tc in result["tool_calls"] if tc["name"] == et]
        actual = matching[0]["args"] if matching else {}
        for k, v in scenario["expect_args"].items():
            actual_val = str(actual.get(k, ""))
            ok = v in actual_val
            validations.append({
                "criterio": f"arg {k}={v}",
                "status": "PASS" if ok else "FAIL",
                "detalhe": "" if ok else f"real: {actual_val}",
            })

    # Not contains
    for term in scenario.get("expect_not_contains", []):
        ok = term.lower() not in result["response"].lower()
        validations.append({
            "criterio": f"NÃO contém '{term}'",
            "status": "PASS" if ok else "FAIL",
            "detalhe": "" if ok else "presente na resposta",
        })

    return validations


def build_flow_nodes(scenario: dict, result: dict, validations: list) -> list:
    all_pass = all(v["status"] == "PASS" for v in validations)
    status = "PASS" if all_pass else "FAIL"

    tc_str = "nenhuma"
    if result["tool_calls"]:
        parts = []
        for tc in result["tool_calls"]:
            args = ", ".join(f'{k}="{v}"' for k, v in tc["args"].items())
            parts.append(f"{tc['name']}({args})")
        tc_str = "; ".join(parts)

    return [
        {
            "id": 1, "tipo": "INPUT", "label": scenario["nome"],
            "dados": {
                "mensagem": scenario["input"],
                "contexto": scenario.get("context") or "nenhum",
            },
        },
        {
            "id": 2, "tipo": "GRAFO", "label": "Invocação graph.ainvoke()",
            "dados": {"modelo": "gemini-2.0-flash"},
        },
        {
            "id": 3, "tipo": "OUTPUT_IA", "label": "O que o modelo retornou",
            "dados": {
                "raw_messages": result["raw_messages"],
                "content": result["response"][:500],
                "tool_calls": result["tool_calls"],
                "tool_calls_str": tc_str,
            },
        },
        {
            "id": 4, "tipo": "VALIDACAO", "label": "Checagens",
            "dados": {"criterios": validations},
        },
        {
            "id": 5, "tipo": "RESULTADO", "label": status,
            "dados": {"resultado": status, "tempo_ms": result["duration_ms"]},
        },
    ]


async def main():
    print("=" * 60)
    print("Tool Calls Esperados — 8 cenários contra grafo real")
    print("=" * 60)

    flows = []
    total_pass = 0
    total_fail = 0

    for sc in SCENARIOS:
        print(f"\n[{sc['id']}] {sc['nome']}...")
        try:
            result = await run_one(sc)
            validations = validate(sc, result)
            all_pass = all(v["status"] == "PASS" for v in validations)
            status = "PASS" if all_pass else "FAIL"

            if all_pass:
                total_pass += 1
            else:
                total_fail += 1

            # Print
            color = "\033[92m" if all_pass else "\033[91m"
            reset = "\033[0m"
            tc_names = ", ".join(tc["name"] for tc in result["tool_calls"]) or "nenhuma"
            print(f"  Tool calls: {tc_names}")
            if result["tool_calls"]:
                for tc in result["tool_calls"]:
                    args = ", ".join(f'{k}="{v}"' for k, v in tc["args"].items())
                    print(f"    → {tc['name']}({args})")
            print(f"  Resposta: {result['response'][:200]}")
            for v in validations:
                icon = "+" if v["status"] == "PASS" else "X"
                det = f" — {v['detalhe']}" if v["detalhe"] else ""
                print(f"  [{icon}] {v['criterio']}{det}")
            print(f"  {color}{status}{reset} ({result['duration_ms']}ms)")

            nodes = build_flow_nodes(sc, result, validations)
            flows.append({
                "id": sc["id"],
                "nome": sc["nome"],
                "status": status,
                "nos": nodes,
            })

        except Exception as e:
            print(f"  ERRO: {e}")
            total_fail += 1
            flows.append({
                "id": sc["id"],
                "nome": sc["nome"],
                "status": "ERROR",
                "erro": str(e),
            })

    # Salvar
    output = {
        "flow_name": "tool_calls_esperados",
        "data": datetime.now().strftime("%Y-%m-%d"),
        "total": len(SCENARIOS),
        "pass": total_pass,
        "fail": total_fail,
        "cenarios": flows,
    }

    out_path = PROJECT_ROOT / "tests" / "flows" / "tool_calls_esperados.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))

    print(f"\n{'=' * 60}")
    print(f"TOTAL: {len(SCENARIOS)} | PASS: {total_pass} | FAIL: {total_fail}")
    print(f"Salvo em: {out_path}")
    print(f"{'=' * 60}")

    sys.exit(1 if total_fail > 0 else 0)


if __name__ == "__main__":
    asyncio.run(main())
