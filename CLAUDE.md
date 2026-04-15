# CLAUDE.md — Ana LangGraph

## Propósito do agente

Ana é o agente de WhatsApp da **Aluga-Ar** (locação de ar-condicionado). Roda em LangGraph + Gemini e atende no canal Leadbox (WhatsApp Cloud API). Responsabilidades em produção:

1. **Atender leads** — responde dúvidas, qualifica, consulta cadastro do cliente no Asaas (via CPF/telefone).
2. **Disparar cobranças automáticas** — régua de dias úteis via `billing_job.py` (cron 9h, seg-sex), usando cobranças PENDING/OVERDUE sincronizadas do Asaas.
3. **Disparar manutenção preventiva** — `manutencao_job.py` envia lembretes D-7 antes da próxima manutenção.
4. **Transferir quando necessário** — handoff para filas humanas (Atendimento, Financeiro, Cobranças) via Leadbox API.
5. **Registrar compromissos de pagamento** — quando o cliente promete uma data, silencia cobranças automáticas até lá.

Produção: `https://ana.fazinzz.com` · PM2: `ana-langgraph` · Porta: `3202`

---

## Caminho rápido por sintoma

| Sintoma | Onde mexer |
|---|---|
| Resposta errada da IA | `core/prompts.py` (prompt) ou `core/grafo.py` (guardrails) |
| Tool não chamada / hallucination | `core/hallucination.py` + `core/grafo.py` |
| Transferência errada | `core/tools.py` (transferir_departamento) + `core/prompts.py` |
| Cobrança/billing errado | `jobs/billing_job.py` + `core/context_detector.py` |
| Manutenção errada | `jobs/manutencao_job.py` + `core/context_detector.py` |
| Não pausou / IA respondeu humano | `api/webhooks/leadbox.py` (fromMe 3 camadas) + `infra/buffer.py` |
| Snooze não funcionou | `core/tools.py` (registrar_compromisso) + `jobs/billing_job.py` |
| Consulta Asaas falhou | `core/tools.py` (consultar_cliente) — status UPPERCASE |
| Webhook Leadbox ignorado | `api/webhooks/leadbox.py` — TENANT_ID, token query param |
| Incidente não registrado | `infra/incidentes.py` |

---

## Mapa do projeto — onde está cada coisa

### `api/` — entrada HTTP
| Arquivo | Responsabilidade |
|---|---|
| `api/app.py` | Entry point FastAPI (porta 3202), monta rotas do webhook |
| `api/webhooks/leadbox.py` | Webhook Leadbox: handlers de `NewMessage`, `QueueChange`, `FinishedTicket`, `UpdateOnTicket`. fromMe em 3 camadas (marker Redis → sendType API → humano). Chama `MessageBuffer` para agrupar mensagens antes de processar |

### `core/` — cérebro do agente
| Arquivo | Responsabilidade |
|---|---|
| `core/grafo.py` | Grafo LangGraph ReAct. `processar_mensagens()` = entry, `call_model()` = nó do LLM, `_context_extra` (dict global por lead) injeta contexto billing/manutenção |
| `core/tools.py` | 3 tools: `consultar_cliente` (Asaas), `transferir_departamento` (Leadbox PUSH), `registrar_compromisso` (snooze billing) |
| `core/prompts.py` | System prompt da Ana + IDs de filas/usuários para transferência |
| `core/constants.py` | Tabelas Supabase, IDs Leadbox (`TENANT_ID=123`, `QUEUE_IA=537`, `QUEUE_BILLING=544`, `QUEUE_MANUTENCAO=545`, `USER_IA=1095`), credenciais Leadbox |
| `core/context_detector.py` | `detect_context()` acha último disparo billing/manutenção no histórico. `build_context_prompt()` injeta no system prompt |
| `core/auto_snooze.py` | `auto_snooze_billing()` — aplica snooze automático após interação em contexto billing |
| `core/hallucination.py` | Detector de hallucination + interceptor de tool-como-texto (evita Ana dizer "transferindo" sem chamar tool) |

### `infra/` — integrações externas
| Arquivo | Responsabilidade |
|---|---|
| `infra/supabase.py` | `get_supabase()` singleton |
| `infra/redis.py` | `RedisService`: pause (`is_paused`, `pause_set/clear`), snooze (`is_snoozed`, `snooze_set/get`), lock, buffer |
| `infra/buffer.py` | `MessageBuffer` — agrupa mensagens por 9s antes de processar (cap 20 msgs) |
| `infra/nodes_supabase.py` | `upsert_lead`, `buscar_historico`, `salvar_mensagem`, `salvar_mensagens_agente` |
| `infra/leadbox_client.py` | `enviar_resposta_leadbox` (texto livre) + `enviar_template_leadbox` (template Meta) + `_mark_sent_by_ia` (marker anti-eco) |
| `infra/event_logger.py` | `log_event()` → `logs/events.jsonl` (rotação 5MB). Usado por webhook, jobs e grafo |
| `infra/incidentes.py` | `registrar_incidente()` → tabela `ana_incidentes` (tipos: `billing_erro`, `envio_falhou`, `hallucination`, `snooze_falhou`, etc.) |
| `infra/retry.py` | Retry exponencial para invocação do grafo (3 tentativas Gemini) |

### `jobs/` — disparos automáticos (cron PM2)
| Arquivo | Responsabilidade |
|---|---|
| `jobs/billing_job.py` | Cron 9h seg-sex. Lê `asaas_cobrancas` PENDING/OVERDUE, aplica régua `SCHEDULE=[0,1,3,5,7,10,15]`, envia template WhatsApp via `enviar_template_leadbox`, marca `ia_cobrou` na cobrança |
| `jobs/manutencao_job.py` | Cron 9h seg-sex. Disparo D-7 antes da manutenção preventiva |

### `scripts/`, `tests/`, `docs/`, `logs/`
- `scripts/resumo.py` — diagnóstico standalone sobre `events.jsonl`
- `tests/` — ver `tests/INDICE.md` para mapa
- `docs/` — `FLUXO_MENSAGEM.md`, `OPERACOES.md`, `TROUBLESHOOTING.md`
- `logs/` — runtime (gitignored): `events.jsonl`, `webhook_payloads.jsonl`

### Raiz
| Arquivo | Responsabilidade |
|---|---|
| `ecosystem.config.js` | PM2: `ana-langgraph` (API), `ana-billing-job` (cron), `ana-manutencao-job` (cron) |
| `CLAUDE.md` | Este arquivo |
| `MEMORY.md` | Memória persistente entre sessões |
| `NAO_MEXER.md` | Acoplamentos perigosos |

---

## Fluxo de mensagem recebida (resumo)

1. **Leadbox** → webhook `api/webhooks/leadbox.py` valida TENANT_ID + token
2. **fromMe** detectado em 3 camadas (marker Redis → `sendType=="API"` → humano pausa IA)
3. **Buffer 9s** (`infra/buffer.py`) agrupa mensagens — evita processar rajada
4. **Lock Redis** por telefone impede processamento paralelo
5. **`processar_mensagens()`** em `core/grafo.py` → detecta contexto (billing/manutenção) → invoca grafo ReAct
6. **Tools** chamadas conforme intenção: `consultar_cliente`, `transferir_departamento`, `registrar_compromisso`
7. **Resposta** enviada via `enviar_resposta_leadbox` + marker Redis anti-eco
8. **Histórico** salvo em `ana_leads.conversation_history` (JSONB inline)

---

## Fluxo de disparo de cobrança (billing)

Ordem de execução real em produção:

| # | Onde | O que faz |
|---|---|---|
| 1 | `ecosystem.config.js` (L30-43) | PM2 cron `0 9 * * 1-5` roda `billing_job.py` |
| 2 | `jobs/billing_job.py::run_billing` | Adquire lock `lock:billing_job`, loop de envio |
| 3 | `jobs/billing_job.py::buscar_elegiveis` | Query `asaas_cobrancas` PENDING/OVERDUE + join `asaas_clientes`, aplica `SCHEDULE=[0,1,3,5,7,10,15]` |
| 4 | `jobs/billing_job.py::_processar_disparo` | Pause check → snooze Redis → snooze Supabase fallback (`ana_leads.billing_snooze_until`) → anti-duplicata |
| 5 | `infra/nodes_supabase.py::upsert_lead` | Cria lead se não existe |
| 6 | `jobs/billing_job.py` (L291-304) | Salva contexto no `conversation_history` ANTES de enviar |
| 7 | `infra/leadbox_client.py::enviar_template_leadbox` | POST Meta API + registra mensagem no Leadbox na fila 544 (user 1095) |
| 8 | `jobs/billing_job.py` (L326-339) | Marca `ia_cobrou=true`, `ia_total_notificacoes++` na `asaas_cobrancas` |
| 9 | `infra/event_logger.py::log_event` | `billing_sent` / `billing_skipped` / `billing_error` em `events.jsonl` |
| 10 | `infra/incidentes.py::registrar_incidente` | Se falhar, grava `billing_erro` em `ana_incidentes` |

**Snooze** (`core/tools.py::registrar_compromisso` L362-458): quando cliente promete pagar, grava `billing_snooze_until` em `ana_leads` + Redis. O job no passo 4 lê ambos.

---

## Fluxo de disparo de manutenção preventiva (D-7)

Ordem de execução real em produção:

| # | Onde | O que faz |
|---|---|---|
| 1 | `ecosystem.config.js` (L16-29) | PM2 cron `0 9 * * 1-5` roda `manutencao_job.py` |
| 2 | `jobs/manutencao_job.py::run_manutencao` (L120-159) | Pula fim de semana, adquire lock `lock:manutencao_job`, loop de envio |
| 3 | `jobs/manutencao_job.py::buscar_contratos_d7` (L44-117) | Query `contract_details` onde `proxima_manutencao = hoje+7`, pula `maintenance_status=notified`, busca telefone no contrato ou em `asaas_clientes` |
| 4 | `jobs/manutencao_job.py` (L34-41) | `TEMPLATE` (texto livre, não é template Meta) com nome, equipamento, endereço |
| 5 | `jobs/manutencao_job.py::_processar_notificacao` (L162-251) | Pause check → anti-duplicata `dispatch:{phone}:manutencao_preventiva:{contract_id}:{date}` |
| 6 | `infra/nodes_supabase.py::upsert_lead` | Cria lead se não existe |
| 7 | `jobs/manutencao_job.py` (L213-226) | Salva contexto `manutencao_preventiva` em `conversation_history` ANTES de enviar |
| 8 | `jobs/manutencao_job.py` (L229-235) | Marca `contract_details.maintenance_status = "notified"` + `notificacao_enviada_at` |
| 9 | `infra/leadbox_client.py::enviar_resposta_leadbox` | Envia texto livre (`raw=True`) na fila `QUEUE_MANUTENCAO=545` (user `USER_IA=1095`) |
| 10 | `infra/event_logger.py::log_event` | `manutencao_sent` em `events.jsonl` |
| 11 | `infra/incidentes.py::registrar_incidente` | Se falhar, grava `manutencao_erro` em `ana_incidentes` com `contract_id` |

**Diferenças vs billing:**
- Usa **texto livre** (`enviar_resposta_leadbox raw=True`), não template Meta — billing usa `enviar_template_leadbox`
- Fonte é `contract_details`, não `asaas_cobrancas`
- Sem régua de dias úteis — só D-7 exato
- Sem snooze — controle via coluna `maintenance_status`
- Contexto salvo no histórico: `manutencao_preventiva` (lido por `core/context_detector.py` quando lead responde)

---

## Tools (3 ativas)

| Tool | Arquivo | O que faz |
|---|---|---|
| `consultar_cliente` | `core/tools.py` | Busca Asaas por CPF/telefone: dados, cobranças, contratos. Salva vínculo CPF/`asaas_customer_id` em `ana_leads` |
| `transferir_departamento` | `core/tools.py` | POST PUSH no Leadbox com `queue_id` + `user_id` |
| `registrar_compromisso` | `core/tools.py` | Valida data (máx 30 dias), grava `billing_snooze_until` em `ana_leads` |

**IDs de transferência** vivem em 2 lugares (atualizar AMBOS): `core/prompts.py` e docstring de `transferir_departamento` em `core/tools.py`.
- Atendimento: queue 453, user 815 (Nathália) ou 813 (Lázaro)
- Financeiro: queue 454, user 814 (Tieli)
- Cobranças: queue 544, user 814

---

## Supabase

**Uma tabela só para leads:** `ana_leads` com `conversation_history` JSONB inline.

Colunas usadas:
```
telefone, nome, cpf, asaas_customer_id, conversation_history,
current_state, current_queue_id, current_user_id, ticket_id,
paused_at, paused_by, responsavel, handoff_at, transfer_reason,
last_interaction_at, updated_at, billing_snooze_until
```

**Tabelas Asaas (somente leitura — sincronizadas pelo lazaro-real):**
```
asaas_clientes, asaas_cobrancas, asaas_contratos,
billing_notifications, contract_details
```
> Dados desatualizados (pagou mas status PENDING) = problema no sync do lazaro-real.

**Tabela de falhas:** `ana_incidentes` (escrita por `infra/incidentes.py`).

---

## Redis — 7 chaves

```
AGENT_ID = "ana-langgraph"

buffer:msg:ana-langgraph:{phone}            → mensagens acumuladas (TTL 300s)
lock:msg:ana-langgraph:{phone}              → lock anti-paralelo (TTL 60s)
pause:ana-langgraph:{phone}                 → IA pausada (sem TTL)
context:ana-langgraph:{phone}               → contexto de mídia (TTL 300s)
snooze:billing:ana-langgraph:{phone}        → snooze billing (TTL auto)
sent:ia:ana-langgraph:{phone}               → marker anti-eco IA (TTL 15s)
dispatch:{phone}:{context}:{ref}:{date}     → anti-duplicata disparo (TTL 86400s)
lock:billing_job                            → lock do cron billing (TTL 3600s)
```

---

## Stack e variáveis

**Stack:** Gemini 2.0 Flash (via LangGraph) · FastAPI 3202 · Leadbox (WhatsApp Cloud API) · Supabase · Redis · PM2

| Variável | Obrigatória | Uso |
|---|---|---|
| `GOOGLE_API_KEY` | Sim | Gemini |
| `SUPABASE_URL` / `SUPABASE_KEY` | Sim | Banco |
| `REDIS_URL` | Sim | Cache, buffer, lock, pausa |
| `LEADBOX_API_URL` / `LEADBOX_API_UUID` / `LEADBOX_API_TOKEN` | Sim | Envio Leadbox |
| `ADMIN_PHONE` | Não | Alertas WhatsApp |
| `AGENT_ID` | Não | Prefixo Redis (default `ana-langgraph`) |
| `GEMINI_MODEL` | Não | Default `gemini-2.0-flash` |

---

## Regras invioláveis

- **Uma tabela só** (`ana_leads`) com histórico JSONB inline
- **IDs de filas/usuários em 2 lugares** (`core/prompts.py` + docstring `transferir_departamento`) — atualizar AMBOS
- **Constantes em `core/constants.py`** — nunca hardcodar IDs
- **Token Leadbox = query param** `?token=JWT`, não header Bearer
- **Buffer 9s obrigatório** — nunca processar direto do webhook
- **Pausa via Redis** — webhook controla (QueueChange + FinishedTicket)
- **Contexto billing/manutenção detectado 1x** em `processar_mensagens()`, não no loop ReAct
- **Asaas status em UPPERCASE**: `PENDING`, `OVERDUE`, `RECEIVED`, `CONFIRMED`, `ACTIVE`, `INACTIVE`
- **Ticket fechado**: confiar só em `event=FinishedTicket` ou `ticket.status=closed`. `UpdateOnTicket` com `queue_id=None` NÃO é fechamento
- **fromMe**: NUNCA usar fila. Usar `sendType` do payload (`"API"` = IA)

---

## Armadilhas conhecidas (ler antes de editar)

1. **`_get_supabase()` em `core/tools.py` é separada** do singleton de `infra/supabase.py`. Tools usam instância própria.
2. **`enviar_resposta_leadbox` vive em `infra/leadbox_client.py`**, não em `api/webhooks/leadbox.py`. Webhook re-importa de lá.
3. **Jobs (`billing_job`, `manutencao_job`) usam a mesma `enviar_resposta_leadbox`/`enviar_template_leadbox`** do webhook.
4. **`_context_extra` em `grafo.py` é dict global.** Funciona só porque cada chamada é sequencial por lead (lock Redis).
5. **`IA_QUEUES = {537, 544, 545}`** — IA responde nas 3. Transferir para 544/545 NÃO pausa. Só filas fora (453, 454) pausam.
6. **Todos os pontos que enviam pro Leadbox DEVEM gravar `_mark_sent_by_ia`** (marker anti-eco).
7. **`MAX_TOOL_ROUNDS` conta só após último HumanMessage** da invocação atual — não histórico antigo.

---

## Pendência: migração gemini-2.5-flash (deadline 2026-06-01)

Google desliga `gemini-2.0-flash` em 01/06/2026. Modelo configurável via `GEMINI_MODEL` em `core/grafo.py`.

**Regressões conhecidas** (testado 2026-04-10):
- R2: "quero falar com o financeiro" → 2.5 responde template em vez de transferir
- R6: "ar fazendo barulho" em contexto manutenção → 2.5 responde template
- X4: "quero falar com um atendente" → 2.5 não chama tool

**Antes de migrar:** corrigir prompt → rodar suite 3x com `GEMINI_MODEL=gemini-2.5-flash` → comparar com baseline `tests/results/all_20260410.json` (62/76 PASS com 2.0-flash).

---

## Relação com Ana original (lazaro-real)

| | Ana original | Ana LangGraph |
|---|---|---|
| Porta | 3115 | 3202 |
| PM2 | `lazaro-ia` | `ana-langgraph` |
| LLM | Gemini direto | Gemini via LangGraph |
| Tabela | `LeadboxCRM_Ana_14e6e5ce` | `ana_leads` |
| Histórico | `leadbox_messages_Ana_14e6e5ce` | `ana_leads.conversation_history` |

> As duas NÃO podem receber webhooks ao mesmo tempo no Leadbox.

---

*Detalhes: `docs/FLUXO_MENSAGEM.md` · `docs/OPERACOES.md` · `docs/TROUBLESHOOTING.md` · `NAO_MEXER.md` · `.claude/GOVERNANCE.md`*
