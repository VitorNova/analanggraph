#!/usr/bin/env python3
"""Teste do interceptor de tool-as-text em processar_mensagens().

Simula o cenário real: graph.ainvoke() retorna um AIMessage com
content="transferir_departamento(queue_id=453, user_id=815)" e tool_calls=[].
Verifica se o interceptor em grafo.py detecta e bloqueia o envio ao cliente.

NÃO mocka transferir_departamento — deixa o interceptor tentar executar.
Mocka apenas: invocar_com_retry (para injetar o AIMessage ruim),
Redis, Supabase, e Leadbox HTTP (para não fazer chamadas reais).
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

# Setup
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from langchain_core.messages import AIMessage, HumanMessage

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("test_interceptor_real")

PHONE = "5565999990000"


def make_bad_graph_result(input_messages):
    """Cria resultado do grafo simulando o bug: tool-as-text no content."""
    bad_ai = AIMessage(
        content="transferir_departamento(queue_id=453, user_id=815)",
        tool_calls=[],  # <-- BUG: deveria ter tool_calls preenchido
    )
    return {
        "messages": input_messages + [bad_ai]
    }


async def run_test():
    """Roda processar_mensagens com um resultado de grafo contendo tool-as-text."""

    # Mock do invocar_com_retry para retornar nosso AIMessage ruim
    async def fake_invocar(graph, state, phone=None):
        result = make_bad_graph_result(state["messages"])
        return result, None

    # Mock do Redis
    mock_redis = AsyncMock()
    mock_redis.is_paused.return_value = False
    mock_redis.pause_set = AsyncMock()

    # Tracking de chamadas ao Leadbox
    leadbox_calls = []

    def fake_enviar(phone, msg, queue_id=None, user_id=None):
        leadbox_calls.append({
            "phone": phone,
            "msg": msg[:200],
            "queue_id": queue_id,
            "user_id": user_id,
        })
        logger.info(f"[LEADBOX MOCK] phone={phone} queue={queue_id} user={user_id} msg={msg[:80]}")

    def fake_mark_sent(phone):
        logger.info(f"[MARKER MOCK] {phone}")

    # Mock do Supabase para buscar_historico e salvar
    def fake_buscar_historico(phone, limite=20):
        return []

    def fake_salvar_mensagem(phone, texto, direction):
        pass

    def fake_salvar_mensagens_agente(phone, msgs, usage=None):
        pass

    # Mock de incidentes — captura registros
    incidentes = []
    def fake_registrar_incidente(phone, tipo, detalhe, contexto=None):
        incidentes.append({"phone": phone, "tipo": tipo, "detalhe": detalhe})
        logger.info(f"[INCIDENTE] tipo={tipo} detalhe={detalhe[:80]}")

    # Mock do event logger
    events = []
    def fake_log_event(event, phone, **kwargs):
        events.append({"event": event, "phone": phone, **kwargs})
        logger.info(f"[EVENT] {event} phone={phone} {kwargs}")

    # Aplicar todos os mocks e rodar processar_mensagens
    with patch("infra.retry.invocar_com_retry", side_effect=fake_invocar), \
         patch("infra.redis.get_redis_service", return_value=mock_redis), \
         patch("infra.nodes_supabase.buscar_historico", side_effect=fake_buscar_historico), \
         patch("infra.nodes_supabase.salvar_mensagem", side_effect=fake_salvar_mensagem), \
         patch("infra.nodes_supabase.salvar_mensagens_agente", side_effect=fake_salvar_mensagens_agente), \
         patch("infra.leadbox_client.enviar_resposta_leadbox", side_effect=fake_enviar), \
         patch("infra.leadbox_client._mark_sent_by_ia", side_effect=fake_mark_sent), \
         patch("infra.incidentes.registrar_incidente", side_effect=fake_registrar_incidente), \
         patch("infra.event_logger.log_event", side_effect=fake_log_event), \
         patch("core.auto_snooze.auto_snooze_billing", new_callable=AsyncMock), \
         patch("core.grafo.ADMIN_PHONE", None):

        # Mockar Supabase fail-safe (query de fila)
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
        with patch("infra.supabase.get_supabase", return_value=mock_sb):

            from core.grafo import processar_mensagens

            logger.info("=" * 60)
            logger.info("CENÁRIO: Gemini retorna tool-as-text no content")
            logger.info("  content = 'transferir_departamento(queue_id=453, user_id=815)'")
            logger.info("  tool_calls = []")
            logger.info("=" * 60)

            await processar_mensagens(
                phone=PHONE,
                messages=[{"texto": "Meu ar tá pingando"}],
            )

    # Análise dos resultados
    print("\n" + "=" * 60)
    print("RESULTADO DO TESTE")
    print("=" * 60)

    print(f"\n1. Incidentes registrados: {len(incidentes)}")
    for inc in incidentes:
        print(f"   tipo={inc['tipo']} detalhe={inc['detalhe'][:100]}")

    print(f"\n2. Eventos logados: {len(events)}")
    for ev in events:
        print(f"   {ev['event']} {({k:v for k,v in ev.items() if k not in ('event','phone')})}")

    print(f"\n3. Chamadas ao Leadbox: {len(leadbox_calls)}")
    for call in leadbox_calls:
        print(f"   phone={call['phone']} queue={call['queue_id']} user={call['user_id']}")
        print(f"   msg={call['msg'][:100]}")

    # Verificações
    print("\n" + "=" * 60)
    print("VERIFICAÇÕES")
    print("=" * 60)

    # V1: interceptor detectou?
    tool_text_events = [e for e in events if e["event"] == "tool_as_text_blocked"]
    print(f"\n✓ Interceptor detectou tool-as-text? {'SIM' if tool_text_events else 'NÃO'}")
    if tool_text_events:
        print(f"  Evento: {tool_text_events[0]}")

    # V2: incidente registrado?
    tool_text_incidentes = [i for i in incidentes if i["tipo"] == "tool_como_texto"]
    print(f"✓ Incidente 'tool_como_texto' registrado? {'SIM' if tool_text_incidentes else 'NÃO'}")

    # V3: texto original NÃO foi enviado ao cliente?
    envios_ao_cliente = [c for c in leadbox_calls if c["phone"] == PHONE]
    texto_tool_enviado = any("transferir_departamento" in c["msg"] for c in envios_ao_cliente)
    print(f"✓ Texto 'transferir_departamento(...)' enviado ao cliente? {'SIM ← BUG!' if texto_tool_enviado else 'NÃO ← correto'}")

    # V4: transferência foi executada diretamente?
    recovery_events = [e for e in events if e["event"] == "tool_as_text_recovered"]
    print(f"✓ Transferência executada via interceptor? {'SIM' if recovery_events else 'NÃO'}")

    # V5: fallback enviado?
    fallback_calls = [c for c in envios_ao_cliente if "erro interno" in c["msg"].lower() or "tente novamente" in c["msg"].lower()]
    print(f"✓ Fallback genérico enviado? {'SIM' if fallback_calls else 'NÃO'}")

    # Resumo
    print("\n" + "=" * 60)
    bloqueou = bool(tool_text_events) and not texto_tool_enviado
    print(f"RESULTADO FINAL: {'PASS — interceptor bloqueou e agiu' if bloqueou else 'FAIL — interceptor não funcionou'}")
    print("=" * 60)

    return bloqueou


if __name__ == "__main__":
    result = asyncio.run(run_test())
    sys.exit(0 if result else 1)
