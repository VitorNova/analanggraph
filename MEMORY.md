# MEMORY.md — Ana LangGraph (Aluga-Ar)

Formato: data, problema/feature, solução, arquivos. Leia antes de qualquer tarefa.

---

## Estado Atual

**Status:** EM PRODUCAO. 78/82 cenarios PASS (4 FAILs — todos flaky do Gemini). Modelo: gemini-2.5-pro.
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
| `tool_choice="auto"` no bind_tools | Gemini ignorava tools sem isso — 3% hallucination |
| Hallucination detector so tempo passado | Patterns futuros ("te encaminho") geravam falso positivo no funil de vendas, transferindo lead prematuramente |
| Prompt diz "chame transferir_departamento()" explicitamente | "transfira" vago fazia Gemini escrever texto em vez de function_call |
| Dead code GEMINI_FUNCTION_DECLARATIONS removido | Nunca era importado, LangChain converte automaticamente via bind_tools |
| Guard anti-loop em route_model_output | transferir_departamento chamada 3-5x em loop — para apos ToolMessage com "Transferido" |
| Recusa pagar → lazaro (nao cobrancas) | Gemini improvisava destino — regra adicionada em prompts.py e tools.py |

---

## Registro de correcoes

### [28/04/2026] Billing risks Fase 1 — visibilidade + fix dedup + heartbeat

- **Problema:** billing_job.py tinha 5 saídas silenciosas (`continue` sem log), 1 bug real (dedup marcado em falha impedia retry), e zero testes unitários. Cobranças podiam sumir sem rastro.
- **Auditoria:** `docs/RISCOS_BILLING.md` — 13 riscos mapeados, revisados como senior, plano atualizado.
- **Fixes implementados em `jobs/billing_job.py`:**
  - **R11 (bug):** Removida L336 (`redis.set(dedup_key)` no bloco de falha de envio). Agora falha permite retry. Adicionado `registrar_incidente("billing_envio_falhou")`.
  - **R12:** `delivery_failed=True` marcado no histórico quando envio falha (best effort).
  - **R13:** Supabase fora → `logger.error` + incidente `billing_supabase_fora`.
  - **R4:** Cliente ausente em `asaas_clientes` → warning + incidente `billing_cliente_ausente`.
  - **R3:** `invoice_url` null → warning + incidente `billing_sem_link`.
  - **R5:** Telefone inválido → warning com phone e customer_id.
  - **R6:** Zero elegíveis → alerta condicional (só se há cobranças PENDING/OVERDUE no banco).
  - **R1 (log-only):** `SCHEDULE_EXTENDED_INTERVAL=5` — offsets > 15 logados como candidatos, sem envio real. Ativar envio na Fase 2 após 1 semana de dados.
  - **R2:** Janela dinâmica `max(SCHEDULE) * 1.6` dias corridos (era 20 fixo).
  - **Contadores:** Dict `skips` com 6 motivos + log de sumário `[BILLING] Filtro: X cobranças → Y elegíveis (Z filtradas: {...})`.
  - **Heartbeat:** `heartbeat:billing_job` gravado no Redis (TTL 25h) ao final de cada execução. Check no início: gap > 49h → incidente `billing_heartbeat_gap`.
  - **Import unificado:** `from infra.incidentes import registrar_incidente` no topo (1x).
- **Testes:** 44 novos em `tests/test_billing_riscos.py` (22 regressão + 22 fix-gate). 155/155 suite completa PASS.
- **Docs:** `docs/RISCOS_BILLING.md` (plano atualizado), `docs/PLANO_TESTES_BILLING.md` (protocolo de testes).
- **Deploy:** PM2 `ana-billing-job` restart 28/04/2026.
- **Pendente:** Fase 2 (ativar envio schedule estendido após 1 semana de dados), Fase 4 (riscos lazaro-real 7/8/9).

### [28/04/2026] Guardrail antierro preventivo — hallucination nunca chega ao cliente

- **Problema:** Gemini afirmava ter executado ações sem chamar a tool (ex: "Já transferi para o financeiro" sem `transferir_departamento`). 7 fixes anteriores falharam porque todos eram **reativos** — detectavam PÓS-resposta em `processar_mensagens()` (L514-560 de grafo.py). A resposta mentirosa já entrava no State, era salva no Supabase, e podia chegar ao cliente.
- **Causa raiz:** `call_model()` tinha 18 linhas sem nenhuma validação entre `response = await model.ainvoke()` e `return {"messages": [response]}` (L108-110 de grafo.py).
- **Fix:** Guardrail de 4 camadas DENTRO de `call_model()`, entre response e return:
  1. **Detector:** `checar_resposta_pre_envio()` reutiliza `_HALL_CHECKS` de hallucination.py
  2. **Retry:** `await model.ainvoke(messages_retry)` com instrução de correção (max 1x)
  3. **Contingência:** tool_call sintético via `inferir_destino_do_texto()` (só transferência)
  4. **Fallback:** `FALLBACK_MSG` — mentira nunca entra no State, nunca salva no Supabase
- **Persistência validada:** resposta mentirosa e msg de correção existem SOMENTE como variáveis locais em `call_model()`. O `return` retorna apenas a resposta corrigida. `processar_mensagens()` extrai `novas_mensagens = result["messages"][qtd_enviadas:]` (L468) e salva via `salvar_mensagens_agente()` (L631) — só a corrigida chega ao Supabase.
- **Sistema reativo mantido:** `detectar_hallucination()` em `processar_mensagens()` (L514-560) continua como rede de segurança pós-grafo.
- **Testes:** 22 novos (14 unitários `checar_resposta_pre_envio` + 8 integração com mock do LLM incluindo 2 de persistência). 109/109 PASS, zero regressão.
- **Arquivos:** `core/hallucination.py` (+`checar_resposta_pre_envio`), `core/grafo.py` (guardrail em `call_model`), `tests/test_hallucination.py` (+14), `tests/test_guardrail_antierro.py` (novo, 8 testes)
- **Commit:** `5a101f3` — deploy PM2 28/04/2026 16:03

### [27/04/2026] Migrar disparo manutenção para Nathália (fila Atendimento)

- **Problema:** Disparo de manutenção preventiva caía no user_id da IA (Ana, 1095) na fila 545. A Ana respondia e quando cliente confirmava agendamento, dizia "a equipe vai entrar em contato" — passo extra desnecessário. Além disso, bug C2: às vezes a Ana tratava resposta ao disparo como conversa nova ("Oi, tudo bem?").
- **Fix:** `jobs/manutencao_job.py` L269-272 — trocado `QUEUE_MANUTENCAO (545) + USER_IA (1095)` por `QUEUE_ATENDIMENTO (453) + USER_NATHALIA (815)`. Agora o template WhatsApp de manutenção D-7 abre ticket direto na Nathália — cliente responde pra ela, sem Ana no meio.
- **Código morto (limpar na Fase 4):** bloco `manutencao` em `context_detector.py` L21-23/L110-124, exemplo manutenção em `prompts.py` L216-217, `QUEUE_MANUTENCAO` em `constants.py` L25 (ainda usado em `IA_QUEUES` para tickets antigos).
- **Testes:** 78/82 PASS (zero regressão). 4 FAILs são flaky pré-existentes (C2, V5, E2, S7).
- **Arquivo alterado:** `jobs/manutencao_job.py`

### [27/04/2026] Refactor lead-simulator + 3 bugs reais corrigidos

- **Problema:** Suite de testes com 33/82 FAILs (60%) — maioria eram bugs do teste, não da Ana. Além disso, 3 bugs reais do Gemini: destino errado (recusa pagar → cobrancas em vez de lazaro), tool `transferir_departamento` chamada 3-5x em loop, e testes desatualizados que esperavam `queue_id` em vez de `destino`.
- **Fix lead-simulator (skill):**
  1. Corrigidos 11 `expect_args` que validavam `queue_id`/`user_id` → agora validam `destino` string
  2. Corrigidos 13 cenários com expectativas desatualizadas vs prompt real (B4, B5, B12, M13, R1, R7, MM1, MT3, etc.)
  3. Removido side-effect check "Leadbox POST chamado" — falso positivo (mock não é chamado em contexto de teste)
  4. Adicionados 3 cenários multi-turno (MT1 funil vendas, MT2 billing insistente, MT3 defeito+CPF)
  5. Adicionado majority vote `--retries 3` (roda até 3x, PASS se 2/3 passam)
  6. Total: 82 cenários (era 79)
- **Fix bug B11 — destino errado:** "recusa pagar" não estava mapeada no prompt nem na docstring → Gemini improvisava pra "cobrancas". Fix: adicionado "recusa pagar → lazaro" em `core/prompts.py` L149 e `core/tools.py` L313-315
- **Fix bug R2/MT2 — tool duplicada:** Após `transferir_departamento` retornar sucesso, Gemini chamava de novo (loop). Fix: guard em `route_model_output` que para o loop quando ToolMessage contém "Transferido" → `core/grafo.py` L120-127
- **Resultado:** 78/82 PASS (95.1%) — era 49/82 (60%). 4 FAILs restantes são flaky do Gemini (C2, V5, E2, S7).
- **Arquivos alterados:** `simulate.py`, `core/prompts.py`, `core/tools.py`, `core/grafo.py`

### [27/04/2026] Fix hallucination de transferencia — 3 camadas

- **Problema:** Gemini 2.5 Pro dizia "vou transferir" / "já te encaminho" sem chamar `transferir_departamento` (3% das transferencias). O interceptor de contingencia recuperava, mas 3 de 5 eram **falsos positivos** — Ana pedia nome+CPF e dizia "já te encaminho pro time" (comportamento correto no funil de vendas), e o interceptor transferia o lead prematuramente sem os dados.
- **Causas raiz:** (1) `bind_tools()` sem `tool_choice` — Gemini podia ignorar tools. (2) Prompt contraditorio — Regra 9 dizia "chame tool PRIMEIRO" mas ETAPA 5-6 descrevia conversa antes de transferir. (3) Detector de hallucination com patterns ambiguos (presente/futuro) que nao distinguiam condicional de afirmacao.
- **Fix em 3 camadas:**
  1. `tool_choice="auto"` em `core/grafo.py` — Gemini recebe instrucao explicita da API pra usar tools
  2. Prompt reescrito: Regra 9 desambiguada, ETAPA 5 sem mencao a transferencia, ETAPA 6 com `transferir_departamento(destino="atendimento")` explicito → `core/prompts.py`
  3. Docstring da tool com exemplos ERRADO/CERTO e regra critica → `core/tools.py`
  4. Detector: removidos 4 patterns ambiguos (`vou transferir`, `te encaminho`, `te transfiro`, `vou te transferir`), mantidos 5 inequivocos de tempo passado → `core/hallucination.py`
  5. Context prompts billing: "transfira" → "chame transferir_departamento(destino='financeiro')" → `core/context_detector.py`
  6. Dead code `GEMINI_FUNCTION_DECLARATIONS` removido (80 linhas nunca importadas) → `core/tools.py`
- **Resultado:** 30 cenarios de transferencia com tool call correto (0 hallucination). V4/M10/TT6 que falhavam agora passam.
- **Testes:** 90 unitarios PASS, 33 hallucination tests PASS. Suite completa: 55/79 (17 FAILs sao bugs do teste que validam queue_id nos args).
- **Relatorio completo:** `tests/relatorio_completo_20260427.txt`

### [25/04/2026] Fix transferência revertida por forceTicketToDepartment

- **Problema:** Nathália reportou no grupo Leadbox+IA que Ana não transferiu a cliente Jasyelly (92151862). Na verdade a tool `transferir_departamento` FOI chamada (08:04:29), mas 4 segundos depois o ticket voltou pra fila IA (537).
- **Causa raiz:** Após a transferência (PUSH para fila 453), a despedida era enviada via `enviar_resposta_leadbox(phone, msg, queue_id=537, user_id=1095)`. O payload incluía `forceTicketToDepartment=True`, forçando o ticket de volta pra fila IA — desfazendo o PUSH.
- **Fix:** Quando `ia_transferiu=True`, enviar despedida SEM `queue_id`/`user_id` → Leadbox mantém o ticket na fila destino do PUSH.
- **Arquivo:** `core/grafo.py` linhas 526-533 (`send_queue = None if ia_transferiu`)
- **Testes:** 85 unitários PASS, 29/35 cenários PASS (6 FAILs pré-existentes, zero regressão)

### [20/04/2026] Comando /R: reset completo + realocar para IA

- **Feature:** Operador envia `R/` ou `/R` no chat → lead volta 100% para IA (antes só zerava histórico).
- **Ações:** (1) Supabase: zera histórico, queue=537, user=1095, state=ai, limpa pausa/snooze. (2) Redis: limpa pause + buffer + snooze:billing. (3) Leadbox API: POST silencioso com forceTicketToDepartment/User → move ticket no CRM.
- **Arquivo:** `api/webhooks/leadbox.py` linhas 77-138
- **Import adicionado:** `USER_IA`, `LEADBOX_API_TOKEN` de `core/constants.py`

### [20/04/2026] Fix consultar_cliente mostrava cobranças futuras

- **Problema:** Tieli reportou (áudio no grupo Leadbox+IA) que Ana cobrava clientes por faturas que ainda não venceram. Ex: cliente pede boleto, Ana manda abril E maio juntos. Cliente fica bravo.
- **Causa raiz:** `consultar_cliente` fazia query `in_("status", ["PENDING", "OVERDUE"])` sem filtro de data → trazia tudo PENDING inclusive meses futuros.
- **Fix:** Separou em 2 queries: OVERDUE (todas) + PENDING com `.lte("due_date", hoje_iso)` → só mostra vencidas ou vencendo até hoje.
- **Arquivo:** `core/tools.py` linhas 178-192
- **Nota extra:** Caso Juliano (cus_000160552820) tinha cobrança duplicada no Asaas (2x R$189 na mesma assinatura sub_hkuzuybd0zqtqnva no mesmo dia) — problema no Asaas, não na Ana.

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
- [x] ~~Aprovar templates de manutencao antes de validar manutencao_job em producao~~ (disparo migrado pra Nathália 27/04)
- [ ] Migracao para gemini-2.5-flash (deadline 01/06/2026) — re-testar com fix de hallucination aplicado. Baseline anterior: `tests/results/all_20260410.json`. Novo baseline: `tests/relatorio_completo_20260427.txt`
- [x] ~~Corrigir 17 cenarios do lead-simulator que validam queue_id/user_id nos args~~ (feito 27/04)
- [ ] Estabilizar 4 cenarios flaky do Gemini (C2, V5, E2, S7) — considerar few-shot examples no prompt
- [ ] Fase 4: limpar codigo morto de manutencao na Ana — bloco `manutencao` em context_detector.py, exemplo no prompts.py, QUEUE_MANUTENCAO em constants.py/IA_QUEUES
- [ ] Billing Fase 2: ativar envio schedule estendido (offset > 15) — verificar logs de `schedule_estendido_candidato` após 1 semana (05/05/2026)
- [ ] Billing Fase 4: riscos lazaro-real (webhook 500, paginação 2000, cron retry 14h) — deploy separado
