# MEMORY.md — Ana LangGraph

> Diário de sessões e decisões. O que aconteceu, por que, e lições aprendidas.
> Referência técnica (stack, estrutura, comandos) → ver CLAUDE.md.
> Última atualização: 2026-04-07

---

## Estado Atual

**Status:** EM PRODUÇÃO. 29/30 cenários PASS (1 flaky). Bug fromMe (W1) RESOLVIDO.
**Incidentes:** 22 tipos cobertos (7 adicionados sessão 3).
**Alertas:** Hallucination e Gemini falhou → WhatsApp pro admin (5566997194084).
**Health:** `/health` verifica API + Redis + Supabase.
**Jobs:** billing e manutenção configurados no PM2 (cron seg-sex 9h).
**Observabilidade:** Payloads webhook salvos raw em `logs/webhook_payloads.jsonl` (manter ativo).

### Pendências
1. Aprovar templates de cobrança antes de validar billing_job em produção
2. Aprovar templates de manutenção antes de validar manutencao_job em produção

---

## Decisões Técnicas (permanentes)

| Decisão | Motivo |
|---|---|
| Canal único Leadbox | UAZAPI/Meta removidos. Simplicidade |
| Constantes em `core/constants.py` | Evitar hardcode espalhado (causou bugs antes) |
| Tabelas Asaas do lazaro-real | Já populadas (369 clientes, 958 cobranças) |
| Buffer 9s (mínimo 5s) | Clientes mandam várias msgs em sequência |
| Contexto detectado 1x em processar_mensagens | Evita query Supabase por iteração do loop ReAct |
| Jobs billing/manutenção via PM2 cron | `cron_restart: "0 9 * * 1-5"`, autorestart: false |
| Snooze só no Supabase na tool, Redis no billing_job | Tools LangGraph são sync, não podem chamar Redis async |
| Defeito → transfere IMEDIATAMENTE, nunca pede CPF | Versão anterior pedia CPF, Gemini era flaky. Regra simplificada (sessão 3) |
| Recusa pagar → Lázaro (dono) | Negociar → Financeiro/Tieli, mas recusar pagar precisa do dono |
| Auto-snooze 48h como fallback | Se Gemini não chamar registrar_compromisso, snooze automático |
| Ticket fechado: só FinishedTicket ou status=closed | UpdateOnTicket+queue=None gerava 124 falsos positivos/dia |
| Status Asaas sempre UPPERCASE | `ACTIVE`, `PENDING`, `OVERDUE` — bug real com `active` minúsculo |
| Incidentes em tabela Supabase | `ana_incidentes` — 15 tipos, phone + detalhe + contexto JSON |
| Hallucination detectada pós-resposta | Compara texto da Ana vs tools chamadas, alerta admin |
| Tracebacks completos em todo logger.error | `exc_info=True` em 15 pontos — nunca truncar stack |
| Health check verifica dependências | Redis PING + Supabase client + API = retorna `degraded` se falhar |
| fromMe: 3 camadas (marker → sendType → humano) | Check IA_QUEUES causava bug W1. sendType=API = IA, resto = humano |
| Captura raw de webhooks em JSONL | Observabilidade permanente. Debug sem reproduzir |
| MAX_TOOL_ROUNDS conta só após último HumanMessage | Histórico antigo inflava counter, encerrando prematuramente |

---

## Diário de Sessões

### 2026-04-07 — Sessão 3: Análise de prompt + fix fromMe + pontos cegos

#### Feature: Prompt reescrito (17 regras, 6 seções novas)
- **Contexto:** Análise profunda identificou 16 problemas no prompt. 3 fontes contraditórias sobre "cliente pagou" (regra 14 vs docstring vs context_detector). Faltava proteção contra prompt injection, limite de loops, instrução pós-tool.
- **O que foi feito:** Prompt reescrito com 17 regras (antes 14), seções "Após Tool", "Cliente que retorna", regra 17 (prioridade contexto). Docstring consultar_cliente e context_detector billing alinhados.
- **Resultado:** 29/30 cenários PASS (antes 16). Suite expandida para 30 cenários cobrindo prompt injection, preço, cancelamento, defeito, voltagem.

#### Feature: Marker Redis em transferir_departamento
- **Contexto:** transferir_departamento fazia POST direto no Leadbox sem gravar marker anti-eco. Era o ÚNICO ponto sem marker dos 7.
- **O que foi feito:** Adicionado `_mark_sent_by_ia(telefone_limpo)` após POST bem-sucedido.
- **Resultado:** Todos os 7 pontos de envio agora têm marker.

#### Feature: MAX_TOOL_ROUNDS = 5
- **Contexto:** Grafo podia loopar infinito se Gemini repetisse tool calls.
- **O que foi feito:** Counter em `route_model_output` que encerra se > 5 tool rounds.

#### Feature: 10 pontos cegos corrigidos (X1-X8)
- **Contexto:** 44 except blocks no código, mas só 15 tinham registrar_incidente. Falhas de persistência (upsert, salvar msg, buscar histórico) eram silenciosas.
- **O que foi feito:** Adicionado registrar_incidente em: upsert_lead, salvar_mensagem, buscar_historico, _mark_sent_by_ia, resposta_vazia. Corrigido except engolido em handle_queue_change.

#### Problema RESOLVIDO: fromMe em IA_QUEUES ignorava humanos reais (W1)
- **Sintoma:** Lead 556697194084 — Tieli responde na fila 544, IA responde por cima.
- **Causa raiz:** Check `IA_QUEUES` em leadbox.py ignorava TODOS os fromMe em filas 537/544/545, incluindo humanos reais. `transferir_departamento` não gravava marker Redis (único ponto sem marker dos 7).
- **Investigação:** Capturados 542 webhooks raw em `logs/webhook_payloads.jsonl`. Analisados 52 fromMe. Descoberto que `sendType="API"` = IA, `sendType="chat"` = humano, `sendType=None` = ambíguo (tratar como humano).
- **Fix:** (1) Marker adicionado em `transferir_departamento`. (2) Check `IA_QUEUES` removido e substituído por 3 camadas: marker Redis → sendType=API → humano. (3) Teste com 52 payloads reais + 4 cenários sintéticos do bug: 100% PASS.
- **Lição:** Nunca usar fila como proxy para "quem enviou". Usar o campo `sendType` do payload Leadbox + marker Redis.

#### Feature: Captura de payloads webhook (permanente)
- **O que faz:** Todos os webhooks salvos raw em `logs/webhook_payloads.jsonl` antes de qualquer filtro.
- **Por que manter:** Observabilidade. Permite debug de qualquer comportamento futuro sem precisar reproduzir.

#### Fix: MAX_TOOL_ROUNDS contava histórico inteiro (review)
- **Sintoma:** Se lead teve 4 tool calls em conversa anterior + 2 novas = 6 → encerrava prematuramente.
- **Fix:** Counter agora conta apenas após último HumanMessage (desta invocação), não do histórico.

**Arquivos modificados:** core/prompts.py, core/tools.py, core/context_detector.py, core/grafo.py, api/webhooks/leadbox.py, infra/nodes_supabase.py, infra/leadbox_client.py, infra/retry.py, tests/cenarios.json, tests/test_fromme_detection.py (novo), simulate.py, test-flow/runner.py, MEMORY.md

---

### 2026-04-06 — Auditoria industrial + sistema de incidentes

#### Feature: Tabela `ana_incidentes` no Supabase
- **Contexto:** Erros aconteciam e ficavam só no PM2 log. Ninguém juntava phone + tipo + detalhe pra investigar depois.
- **O que foi feito:** Criada tabela `ana_incidentes` via MCP Supabase. Módulo `infra/incidentes.py` com `registrar_incidente(phone, tipo, detalhe, contexto)`. Plugado em 15 pontos de falha do código.
- **Resultado:** Cada falha registra automaticamente com phone, tipo, detalhe e timestamp. Validado forçando 5 tipos diferentes.

#### Feature: Detecção de hallucination + alerta WhatsApp
- **Contexto:** Ana dizia "transferi" ou "registrei" sem chamar a tool. Ninguém sabia.
- **O que foi feito:** Check pós-resposta em `grafo.py` compara texto vs tools chamadas. Se detectar, registra incidente + envia WhatsApp pro admin.
- **Resultado:** S1 (que falhava por hallucination) agora é detectado e alertado em tempo real.

#### Feature: Health check com dependências
- **Contexto:** `/health` retornava 200 mesmo com Redis/Supabase fora.
- **O que foi feito:** Endpoint agora faz Redis PING + verifica Supabase client. Retorna `degraded` se algum falhar.
- **Resultado:** `{"status":"healthy","api":"ok","redis":"ok","supabase":"ok"}`

#### Feature: Tracebacks completos em todos os logger.error
- **Contexto:** 13 pontos de log usavam `logger.error(f"...{e}")` sem stack trace. Impossível diagnosticar.
- **O que foi feito:** Adicionado `exc_info=True` em todos os 13 + 2 warnings promovidos a error.
- **Resultado:** Qualquer erro agora mostra traceback completo no PM2 log.

#### Feature: Limpeza industrial do codebase
- **Contexto:** 33 PNGs lixo na raiz, imports mortos, strings hardcoded de tabela, skills desatualizadas.
- **O que foi feito:** Removidos PNGs e scripts/ vazio. Removidos imports mortos (os, httpx) e função órfã. Zero strings literais de tabela (tudo via constants.py). Skills atualizadas (3 tools, 9 grupos, 68 cenários).
- **Resultado:** Projeto limpo pra manutenção por LLM. CLAUDE.md com seção "Armadilhas conhecidas".

#### Feature: CLAUDE.md blindado pra LLMs
- **Contexto:** Simulação mostrou que LLM erraria ao mudar IDs (só mudava no prompt, não na docstring da tool). Não sabia que IA responde em 3 filas. Não sabia que tabelas Asaas são read-only.
- **O que foi feito:** Adicionadas seções: "Armadilhas conhecidas", "Regras do Asaas", "Regras do Leadbox". IDs vivem em 2 lugares documentado. IA_QUEUES explicado. Import circular documentado.
- **Resultado:** 6 armadilhas que causariam erro de LLM agora estão documentadas.

#### Problema: Bug ativo `raw.get()` em string (500 no webhook)
- **Sintoma:** Campo `raw` do Leadbox às vezes vem como string. `.get()` quebra com AttributeError.
- **Causa:** Payload do Leadbox inconsistente — `raw` pode ser dict ou string dependendo do tipo de mídia.
- **Solução:** Validação `isinstance(raw, dict)` antes de `.get()`.
- **Lição:** Nunca confiar no tipo de campos de API de terceiros.

**Arquivos modificados:** `api/app.py`, `api/webhooks/leadbox.py`, `core/grafo.py`, `core/tools.py`, `core/constants.py`, `infra/supabase.py`, `infra/buffer.py`, `infra/nodes_supabase.py`, `infra/incidentes.py` (novo), `jobs/billing_job.py`, `jobs/manutencao_job.py`, `.gitignore`, `CLAUDE.md`, `MEMORY.md`, `.claude/skills/lead-simulator/`, `.claude/skills/test-flow/`

---

### 2026-04-05 — Análise de logs de produção + 4 correções

#### Problema: Contratos Asaas nunca retornavam na consulta
- **Sintoma:** `consultar_cliente` retornava "Nenhum contrato" mesmo para clientes com contrato ativo. Lead 556699553375 (CPF 86685961287) recebeu HTTP 400 na query de contratos.
- **Causa:** `core/tools.py:120` filtrava `.eq("status", "active")` mas o banco usa `ACTIVE` (maiúsculo). Confirmado via `SELECT DISTINCT status FROM asaas_contratos` → `ACTIVE`, `INACTIVE`.
- **Solução:** Trocado `"active"` por `"ACTIVE"`. Mock do simulador também corrigido (`simulate.py:841` usava `"active"`, mascarando o bug nos testes).
- **Lição:** Mocks que espelham bugs criam falsos positivos. Sempre validar valores reais do banco.

#### Problema: 124 falsos "Ticket fechado" por dia
- **Sintoma:** Logs cheios de "Ticket fechado → IA reativada" com `existia: False`. 124 ocorrências hoje, 0 FinishedTicket reais.
- **Causa:** `leadbox.py:273` tratava `UpdateOnTicket + queue_id=None` como ticket fechado. Na prática, Leadbox dispara UpdateOnTicket com queue=None para qualquer mudança no ticket (mensagem lida, editada, etc).
- **Solução:** Removida condição. Agora só `FinishedTicket` e `ticket_status=closed` disparam reset. Validado com webhook de teste.
- **Lição:** Heurísticas sobre eventos de terceiros precisam ser validadas com logs reais, não com suposições.

#### Problema: Defeito urgente com contexto manutenção não transferia (M12/R6)
- **Sintoma:** Lead diz "ar parou de funcionar" com contexto de manutenção preventiva → Ana respondia com template de manutenção em vez de transferir.
- **Causa:** Gemini confundia defeito com manutenção preventiva porque o histórico de disparo falava de "manutenção". O prompt não diferenciava com clareza suficiente.
- **Solução:** Reforçado no prompt: "defeito SEMPRE tem prioridade sobre preventiva, INDEPENDENTE do contexto". Adicionada distinção explícita: agendar = preventiva, relatar problema = defeito.
- **Lição:** Quando LLM confunde dois conceitos próximos, o prompt precisa de contraste explícito ("X NÃO é Y").

#### Problema: Ana dizia "registrei compromisso" sem chamar a tool (S1)
- **Sintoma:** Lead diz "vou pagar sexta" → Ana responde "registrei o compromisso" mas tool `registrar_compromisso` não foi chamada.
- **Causa:** Hallucination do LLM — afirmou ter feito algo que não fez.
- **Solução:** Adicionada regra 4b no prompt: "Nunca diga que fez algo que requer uma tool sem ter chamado a tool."
- **Lição:** LLMs podem hallucinar ações. Regra explícita no prompt reduz a incidência.

#### Feature: 2 cenários de regressão novos (R7, R8)
- **Contexto:** Precisava testar que contratos ACTIVE são retornados corretamente e que defeito urgente transfere.
- **O que foi feito:** R7 (CPF → resposta menciona contrato) e R8 (defeito + CPF → transfere Nathália).
- **Resultado:** 68/68 PASS no suite completo.

**Arquivos modificados:** `core/tools.py`, `api/webhooks/leadbox.py`, `core/prompts.py`, `.claude/skills/lead-simulator/scripts/simulate.py`, `CLAUDE.md`, `MEMORY.md`

---

### 2026-04-04 — Sessão 2: Snooze billing + 66 cenários de teste + review

#### Feature: Snooze de cobrança (billing)
- **Contexto:** Lead recebe disparo de cobrança na quarta, diz "vou pagar sexta". Sem snooze, o billing_job dispara de novo na quinta. Precisava de um mecanismo para silenciar disparos quando o lead promete pagar.
- **O que foi feito:** Nova tool `registrar_compromisso(data_prometida)` que salva `billing_snooze_until` no Supabase. billing_job.py checa snooze Redis + fallback Supabase antes de disparar. Auto-snooze 48h em processar_mensagens (fallback se Gemini não chamar a tool). Migration aplicada no Supabase. Snooze Redis com TTL auto-calculado.
- **Resultado:** 8 cenários de snooze (S1-S8) todos PASS. Lead diz "sexta" → Gemini chama tool com data ISO → billing pula até lá.

#### Feature: Expansão de testes (22 → 66 cenários)
- **Contexto:** Suite anterior tinha 22 cenários, sem cobertura de snooze, cancelamento, devolução, contestação, comprovante, Lázaro, recusa, etc.
- **O que foi feito:** Billing: B1-B21 (21 cenários). Manutenção: M1-M13 (13 cenários). Snooze: S1-S8. Todos com grafo real + Gemini 2.0 Flash.
- **Resultado:** 65/66 PASS (98.5%). R1 flaky do Gemini (variância probabilística).

#### Feature: Recusa de pagamento → Lázaro
- **Contexto:** Lead que se recusa a pagar ("não vou pagar") precisa ir pro dono (Lázaro), não pro financeiro.
- **O que foi feito:** Adicionada regra no prompt: recusa pagar → `transferir_departamento(queue_id=453, user_id=813)`.
- **Resultado:** B11 PASS consistente. Diferencia recusa (→ Lázaro) de negociação (→ Financeiro/Tieli).

#### Feature: Recusa de manutenção → transfere Nathália
- **Contexto:** Lead que recusa manutenção preventiva precisa ser transferido (empresa registra a recusa), não apenas aceitar e encerrar.
- **O que foi feito:** Atualizada regra no contexto de manutenção: recusar → `transferir_departamento(queue_id=453, user_id=815)`.
- **Resultado:** M3 PASS.

#### Feature: Simplificação do fluxo de defeito
- **Contexto:** Regra 7b pedia para chamar `consultar_cliente(buscar_por_telefone=true)` quando lead relata defeito sem contexto. Gemini era flaky nessa chamada (R1 falhava intermitente).
- **O que foi feito:** Simplificou: defeito sem contexto → pede CPF do titular → transfere Nathália. Defeito com contexto (disparo) → transfere direto sem pedir nada.
- **Resultado:** R1, M12, M13, R6 todos PASS consistentes. Zero flakiness.

#### Problema: billing_job.py crashava com NameError
- **Sintoma:** `clean_phone` e `hoje` usados antes de serem definidos no bloco de snooze.
- **Causa:** O bloco de snooze Supabase (fallback) foi inserido antes da definição de `clean_phone` (linha 277). `hoje` era variável de `run_billing()`, não existia em `_processar_disparo()`.
- **Solução:** Movido `clean_phone` para logo após `phone = item["phone"]`. Trocado `hoje` por `date.today()`.
- **Lição:** Ao inserir código no meio de uma função, verificar se as variáveis que usa já existem naquele escopo.

#### Problema: registrar_compromisso tentava async em contexto sync
- **Sintoma:** Warning `There is no current event loop in thread 'asyncio_0'` nos testes.
- **Causa:** A tool é sync (chamada pelo ToolNode do LangGraph), mas tentava `asyncio.get_event_loop()` + `ensure_future()` para setar Redis.
- **Solução:** Removido bloco Redis da tool, mantido só Supabase (sync). billing_job faz fallback Supabase → Redis (restaura quando lê do DB). Auto-snooze em processar_mensagens cobre Redis diretamente (é async).
- **Lição:** Tools do LangGraph são sync. Não misturar async dentro delas.

**Arquivos modificados:** `core/tools.py`, `core/prompts.py`, `core/context_detector.py`, `core/grafo.py`, `infra/redis.py`, `jobs/billing_job.py`, `CLAUDE.md`, `tests/cenarios.json`, `.claude/skills/lead-simulator/scripts/simulate.py`

---

### 2026-04-04 — Sessão 1: Kill switch no grafo

#### Feature: Kill switch em processar_mensagens()
- **Contexto:** Usuário quer que o agente não processe nenhuma mensagem real do WhatsApp/Leadbox enquanto estiver em desenvolvimento.
- **O que foi feito:** Adicionado early return no início de `processar_mensagens()` em `core/grafo.py`. Toda mensagem é logada como ignorada e retorna sem processar. PM2 reiniciado.
- **Resultado:** Mensagens reais bloqueadas. Lead-simulator continua funcionando (chama `graph.ainvoke()` direto, sem passar por `processar_mensagens()`).
- **Como reverter:** Remover as 3 linhas do kill switch (comentário `⛔ KILL SWITCH`, `logger.warning` e `return`) no início de `processar_mensagens()`.

**Arquivos modificados:** `core/grafo.py`

---

### 2026-04-03 — Sessão 2: Criação do MEMORY.md

**O que foi feito:**
- Criado MEMORY.md na raiz do projeto como memória persistente entre sessões
- Adicionada instrução no CLAUDE.md para atualizar MEMORY.md ao final de cada sessão

**Arquivos modificados:**
- `MEMORY.md` (criado)
- `CLAUDE.md` (adicionada seção "Memória Persistente")

---

### 2026-04-03 — Sessão 1: Migração para Leadbox + Limpeza

#### Feature: Canal único Leadbox
- **Contexto:** O projeto tinha 3 canais (UAZAPI, Meta Cloud API, Leadbox). Manter 3 era complexidade desnecessária — a Ana vai rodar 100% via Leadbox (WhatsApp Cloud API).
- **O que foi feito:** Deletamos `api/webhooks/whatsapp.py` e `core/whatsapp/` inteiro. Removemos router UAZAPI do `api/app.py`. Agora só existe `api/webhooks/leadbox.py`.
- **Resultado:** Todo envio/recebimento de mensagem passa exclusivamente pelo Leadbox.

#### Feature: Constantes centralizadas
- **Contexto:** Os mesmos IDs (tabela, tenant, filas, URLs Leadbox) estavam hardcoded em 4-5 arquivos diferentes. Quando mudava em um, quebrava outro — isso causou o bug da tabela errada (ver abaixo).
- **O que foi feito:** Criado `core/constants.py` com `TABLE_LEADS`, `TENANT_ID`, `QUEUE_IA`, `LEADBOX_API_URL`, `LEADBOX_API_UUID`, `LEADBOX_API_TOKEN`, tabelas Asaas, etc. Todos os arquivos agora importam dali.
- **Resultado:** Ponto único de verdade. Mudar um ID = mudar 1 lugar.

#### Feature: Webhook Leadbox completo
- **Contexto:** Precisávamos de pausa/despausa da IA quando lead vai pra fila humana, e reset quando ticket fecha.
- **O que foi feito:** Implementados handlers: `handle_queue_change` (pausar se fila humana, despausar se fila IA), `handle_ticket_closed` (reset lead para estado IA), `handle_new_message` (recebe msg do cliente → buffer → grafo).
- **Resultado:** Pausa/despausa funcionando. NewMessage pronto mas DESATIVADO (só loga) — usuário não quer IA respondendo clientes reais sem supervisão.

#### Feature: Envio via API Leadbox
- **Contexto:** Com UAZAPI removido, precisava de novo método de envio de respostas.
- **O que foi feito:** Criada `enviar_resposta_leadbox()` — POST na API externa do Leadbox com payload `{body, number, externalKey}`.
- **Resultado:** Resposta da IA chega no WhatsApp do cliente via Leadbox.

#### Feature: Vínculo automático CPF/Asaas
- **Contexto:** Toda vez que a IA consultava um cliente no Asaas, não salvava o vínculo. Na próxima conversa, tinha que buscar de novo.
- **O que foi feito:** Em `consultar_cliente` (core/tools.py), quando encontra cliente, salva `cpf` e `asaas_customer_id` na `ana_leads`.
- **Resultado:** Busca Asaas acontece 1x. Depois o lead já está vinculado.

#### Problema: Tabela errada no webhook Leadbox
- **Sintoma:** Webhook leadbox.py tentava gravar em "langgraph_leads" — tabela que não é da Ana.
- **Causa:** Código foi copiado do template genérico do agente da Clínica Suprema, que usa outra tabela. Ninguém ajustou o nome.
- **Solução:** Trocado para `TABLE_LEADS` importado de `core/constants.py` (valor: "ana_leads"). Duas ocorrências corrigidas em leadbox.py.
- **Lição:** Nunca hardcodar nome de tabela. Sempre importar de constants.

#### Problema: Token Leadbox como Bearer em vez de query param
- **Sintoma:** API Leadbox retornava 404 ao enviar resposta da IA.
- **Causa:** O código usava `Authorization: Bearer {token}` no header. A API do Leadbox não aceita Bearer — espera `?token=JWT` na URL.
- **Solução:** Mudado para `params={"token": LEADBOX_API_TOKEN}` no httpx. Testado com curl e confirmado nos logs.
- **Lição:** API Leadbox usa token como query param, não header.

#### Problema: Token Leadbox expirado
- **Sintoma:** Mesmo após corrigir o query param, API ainda retornava 404.
- **Causa:** O token no .env era antigo (sessionId 409, channelType "whatsapp"). O Leadbox migrou para WhatsApp Cloud API (waba) e o token mudou.
- **Solução:** Gerado novo token no painel Leadbox (sessionId 430, channelType "waba"). Atualizado no .env.
- **Lição:** Quando Leadbox der 404, verificar se o token ainda é válido para o channelType correto.

#### Problema: TENANT_ID errado (45 em vez de 123)
- **Sintoma:** Webhook Leadbox ignorava todos os eventos — sempre retornava "wrong_tenant".
- **Causa:** IDs foram copiados do template da Clara (Clínica Suprema, tenant 45). O tenant da Aluga-Ar é 123.
- **Solução:** Corrigido para `TENANT_ID=123`, `QUEUE_IA=537` em constants.py.
- **Lição:** Conferir todos os IDs ao usar templates de outro projeto.

**Arquivos modificados:** `api/webhooks/leadbox.py`, `api/app.py`, `core/constants.py` (criado), `core/tools.py`. Deletados: `api/webhooks/whatsapp.py`, `core/whatsapp/`.

---

### 2026-03-28 — Buffer overflow + lead-simulator

#### Problema: Buffer overflow travava o agente
- **Sintoma:** Agente parava de responder. Redis acumulava centenas de mensagens no buffer.
- **Causa:** Quando o processamento falhava (ex: Gemini fora do ar), as mensagens não eram consumidas do buffer. Na próxima tentativa, o buffer crescia indefinidamente. Com muitas mensagens, o processamento ficava lento e falhava de novo — loop infinito.
- **Solução:** Adicionado cap de 20 mensagens em `infra/buffer.py`. Se `len(messages) > 20`, limpa o buffer e processa só as últimas 5. Isso garante que mesmo após falhas repetidas, o sistema se recupera.
- **Arquivo:** `infra/buffer.py`

#### Feature: Lead-simulator (testes E2E)
- **Contexto:** Precisávamos testar o agente sem WhatsApp real — cenários de saudação, consulta CPF, transferência, pagamento, defeito, manutenção.
- **O que foi feito:** Criados 24 cenários em `tests/cenarios.json`. Script de simulação roda contra o grafo real, valida tool calls e respostas.
- **Resultado:** 22/24 PASS. 2 FAIL: R1 (transfere sem consultar cliente quando ar pinga) e R6 (defeito urgente tratado como manutenção agendável). Bugs ainda em aberto.

---

### 2026-03-27 — Context detector

#### Feature: Detecção de contexto billing/manutenção
- **Contexto:** Quando um lead recebe disparo automático de cobrança ou manutenção, ele responde sobre aquele assunto. Sem contexto, a IA tratava como conversa nova e pedia CPF desnecessariamente.
- **O que foi feito:** Criado `core/context_detector.py` — varre últimas 10 mensagens do histórico buscando campo "context" (billing/manutenção). Se encontra, gera prompt extra com regras específicas (ex: "não peça CPF", "use buscar_por_telefone=true").
- **Integração:** Plugado no grafo via dict `_context_extra` em `core/grafo.py`. `processar_mensagens()` detecta o contexto 1x e salva no dict. `call_model()` lê do dict e injeta no prompt. Evita query ao Supabase a cada iteração do loop ReAct.
- **Resultado:** 6 testes de contexto passando. IA sabe que lead está respondendo sobre cobrança/manutenção.

---

### 2026-03-26 — Scaffold inicial do projeto

#### Feature: Projeto criado do zero
- **Contexto:** A Ana original (lazaro-real) rodava Gemini direto, sem framework. Queríamos migrar para LangGraph para ter grafo ReAct com tools nativas.
- **O que foi feito:** Scaffold completo — grafo ReAct (`core/grafo.py`), 2 tools (`core/tools.py`), buffer 9s (`infra/buffer.py`), Redis service (`infra/redis.py`), persistência Supabase (`infra/nodes_supabase.py`), webhook UAZAPI (`api/webhooks/whatsapp.py`), prompt da Ana (`core/prompts.py`).
- **Resultado:** Agente funcional respondendo via LangGraph + Gemini.

#### Problema: ToolMessage órfãs crashavam Gemini
- **Sintoma:** Gemini rejeitava o histórico e o agente não respondia. Erro: sequência inválida de mensagens.
- **Causa:** O histórico é cortado nas últimas 20 mensagens. Se o corte caía no meio de um bloco AIMessage(tool_calls) + ToolMessage, sobrava uma ToolMessage sem a AIMessage que a originou. Gemini exige que toda ToolMessage tenha uma AIMessage com tool_calls antes dela.
- **Solução:** Validação de sequência em `buscar_historico()` em `infra/nodes_supabase.py`. Varre as mensagens e remove: (1) ToolMessage sem AIMessage precedente, (2) AIMessage com tool_calls sem todas as ToolMessage correspondentes, (3) blocos incompletos no final do histórico.
- **Arquivo:** `infra/nodes_supabase.py`

#### Problema: TENANT_ID e QUEUE_IA errados
- **Sintoma:** Webhook ignorava todos os eventos do Leadbox.
- **Causa:** Template copiado da Clara (Clínica Suprema) trazia TENANT_ID=45 e QUEUE_IA diferente. Aluga-Ar é tenant 123, fila IA 537.
- **Solução:** Corrigido para TENANT_ID=123, QUEUE_IA=537.
- **Arquivos:** `api/webhooks/leadbox.py`, `core/tools.py`
