# MEMORY.md — Ana LangGraph (Aluga-Ar)

Formato: data, problema/feature, solução, arquivos. Leia antes de qualquer tarefa.

---

## Estado Atual

**Status:** EM PRODUCAO. 29/30 cenarios PASS (1 flaky R1).
**Incidentes:** 22 tipos em `ana_incidentes` (Supabase). Hallucination + Gemini falhou → alerta WhatsApp admin.
**Health:** `/health` verifica API + Redis + Supabase.
**Jobs:** billing e manutencao (PM2 cron seg-sex 9h). Templates pendentes aprovacao.

---

## Decisoes Tecnicas

| Decisao | Motivo |
|---|---|
| Canal unico Leadbox | UAZAPI/Meta removidos. Simplicidade |
| Constantes em `core/constants.py` | Evitar hardcode espalhado |
| Tabelas Asaas do lazaro-real | Ja populadas (369 clientes, 958 cobrancas) |
| Buffer 9s (minimo 5s) | Clientes mandam varias msgs em sequencia |
| Contexto detectado 1x em processar_mensagens | Evita query Supabase por iteracao do loop ReAct |
| Jobs billing/manutencao via PM2 cron | `cron_restart: "0 9 * * 1-5"`, autorestart: false |
| Snooze so no Supabase na tool, Redis no billing_job | Tools LangGraph sao sync |
| Defeito → transfere IMEDIATAMENTE, nunca pede CPF | Versao anterior pedia CPF, Gemini era flaky |
| Recusa pagar → Lazaro (dono) | Negociar → Financeiro/Tieli |
| Auto-snooze 48h como fallback | Se Gemini nao chamar registrar_compromisso |
| Ticket fechado: so FinishedTicket ou status=closed | UpdateOnTicket+queue=None gerava 124 falsos positivos/dia |
| Status Asaas sempre UPPERCASE | Bug real com `active` minusculo |
| fromMe: 3 camadas (marker → sendType → humano) | Check IA_QUEUES causava bug W1 |
| Captura raw de webhooks em JSONL | Observabilidade permanente |
| MAX_TOOL_ROUNDS conta so apos ultimo HumanMessage | Historico antigo inflava counter |

---

## Registro de correcoes

### [07/04/2026] Prompt reescrito + fix fromMe + pontos cegos

- Prompt reescrito: 17 regras, 6 secoes novas → 29/30 cenarios PASS (era 16) → `core/prompts.py`
- Marker Redis adicionado em `transferir_departamento` (unico ponto sem marker) → `core/tools.py`
- MAX_TOOL_ROUNDS = 5 em `route_model_output` → `core/grafo.py`
- 10 pontos cegos: registrar_incidente em upsert_lead, salvar_mensagem, buscar_historico, _mark_sent_by_ia, resposta_vazia
- Bug W1 resolvido: fromMe em IA_QUEUES ignorava humanos reais → substituido por 3 camadas (marker → sendType → humano) → `api/webhooks/leadbox.py`
- MAX_TOOL_ROUNDS contava historico inteiro → agora conta so apos ultimo HumanMessage

### [06/04/2026] Auditoria industrial + sistema de incidentes

- Tabela `ana_incidentes` no Supabase + `infra/incidentes.py` plugado em 15 pontos de falha
- Deteccao de hallucination pos-resposta + alerta WhatsApp admin → `core/grafo.py`
- Health check com dependencias (Redis PING + Supabase) → `api/app.py`
- Tracebacks completos (`exc_info=True`) em 15 pontos de log
- Limpeza: 33 PNGs lixo, imports mortos, strings hardcoded → constants.py
- CLAUDE.md: secoes "Armadilhas conhecidas", "Regras do Asaas", "Regras do Leadbox"
- Bug `raw.get()` em string → validacao `isinstance(raw, dict)` → `api/webhooks/leadbox.py`

### [05/04/2026] Analise de logs + 4 correcoes

- Contratos Asaas: `"active"` → `"ACTIVE"` (case sensitive) → `core/tools.py`
- 124 falsos "Ticket fechado"/dia: removida heuristica UpdateOnTicket+queue=None → so FinishedTicket → `api/webhooks/leadbox.py`
- Defeito com contexto manutencao nao transferia → contraste explicito no prompt → `core/prompts.py`
- Ana dizia "registrei compromisso" sem chamar tool (hallucination) → regra 4b no prompt

### [04/04/2026] Snooze billing + 66 cenarios + kill switch

- Tool `registrar_compromisso(data_prometida)` → snooze Redis + Supabase → `core/tools.py`
- Suite expandida: 22 → 66 cenarios (billing B1-B21, manutencao M1-M13, snooze S1-S8)
- Recusa pagar → Lazaro (queue_id=453, user_id=813). Recusa manutencao → Nathalia (815)
- Defeito simplificado: sem contexto → pede CPF → transfere. Com contexto → transfere direto
- billing_job.py crashava: NameError clean_phone/hoje → corrigido escopo
- registrar_compromisso tentava async em sync → removido bloco Redis da tool
- Kill switch em processar_mensagens() para desenvolvimento (3 linhas, removivel)

### [03/04/2026] Migracao Leadbox + constantes

- Canal unico Leadbox: deletados `api/webhooks/whatsapp.py` e `core/whatsapp/`
- `core/constants.py` criado: TABLE_LEADS, TENANT_ID, QUEUE_IA, URLs Leadbox
- Webhook handlers: handle_queue_change, handle_ticket_closed, handle_new_message
- Envio via API Leadbox: POST com body/number/externalKey
- Vinculo automatico CPF/Asaas em consultar_cliente
- Bugs: tabela errada (langgraph_leads → ana_leads), token Bearer → query param, token expirado, TENANT_ID 45 → 123

### [28/03/2026] Buffer overflow + lead-simulator

- Buffer cap de 20 msgs, limpa e processa ultimas 5 se overflow → `infra/buffer.py`
- Lead-simulator: 24 cenarios em `tests/cenarios.json`

### [27/03/2026] Context detector

- `core/context_detector.py`: varre ultimas 10 msgs buscando campo "context" (billing/manutencao)
- Injeta prompt extra via dict `_context_extra` em `core/grafo.py`

### [26/03/2026] Scaffold inicial

- Grafo ReAct, 2 tools, buffer 9s, Redis, Supabase, webhook UAZAPI, prompt Ana
- Fix ToolMessage orfas: validacao de sequencia em `buscar_historico()` → `infra/nodes_supabase.py`
- TENANT_ID e QUEUE_IA corrigidos (template Clara → Ana)

---

## Pendencias

- [ ] Aprovar templates de cobranca antes de validar billing_job em producao
- [ ] Aprovar templates de manutencao antes de validar manutencao_job em producao
- [ ] Migracao para gemini-2.5-flash (deadline 01/06/2026) — 3 regressoes de transferencia (R2, R6, X4). Baseline: `tests/results/all_20260410.json`
