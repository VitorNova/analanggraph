# CLAUDE.md — Ana LangGraph

> Agente Ana (Aluga-Ar) rodando em LangGraph + Gemini.

---

---

## Stack

- **LLM**: Google Gemini 2.0 Flash via LangGraph
- **Framework**: LangGraph (grafo ReAct)
- **API**: FastAPI (porta 3202)
- **Canal**: Leadbox CRM (WhatsApp Cloud API — canal único)
- **Banco**: Supabase (tabela `ana_leads`)
- **Cache/Buffer**: Redis (buffer 9s, lock, pausa)
- **CRM**: Leadbox (tenant 123, queue_ia 537)
- **Deploy**: PM2 (`ana-langgraph`), Produção: `https://ana.fazinzz.com`

---

## Estrutura

```
ana-langgraph/
├── api/
│   ├── app.py                  ← Entry point FastAPI (porta 3202)
│   └── webhooks/
│       └── leadbox.py          ← Webhook Leadbox (handlers de eventos)
├── core/
│   ├── grafo.py                ← LangGraph ReAct (State, graph, processar_mensagens)
│   ├── tools.py                ← consultar_cliente + transferir_departamento + registrar_compromisso
│   ├── constants.py            ← Constantes centralizadas (Leadbox IDs, tabelas, filas)
│   ├── context_detector.py     ← Detecta contexto billing/manutenção no histórico
│   ├── auto_snooze.py          ← Auto-snooze 48h após interação billing
│   ├── hallucination.py        ← Detector de hallucination (tool não chamada)
│   └── prompts.py              ← System prompt da Ana
├── infra/
│   ├── redis.py                ← RedisService (buffer, lock, pause)
│   ├── buffer.py               ← MessageBuffer (delay 9s, cap 20 msgs)
│   ├── supabase.py             ← Client singleton
│   ├── nodes_supabase.py       ← Histórico (buscar/salvar) + upsert lead
│   ├── event_logger.py         ← Logger estruturado (events.jsonl, rotação 5MB)
│   ├── incidentes.py           ← Registro de falhas graves (tabela ana_incidentes)
│   ├── leadbox_client.py       ← Envio de respostas via API Leadbox
│   └── retry.py                ← Retry exponencial para invocação do grafo
├── jobs/
│   ├── billing_job.py          ← Job de cobrança automática (Asaas)
│   └── manutencao_job.py       ← Job de manutenção automática
├── logs/
│   └── resumo.py               ← Script standalone de diagnóstico (events.jsonl)
├── tests/
│   ├── cenarios.json           ← Cenários do lead-simulator
│   └── report.json             ← Último relatório de testes
├── .env                        ← Credenciais
├── ecosystem.config.js         ← PM2 config
├── requirements.txt
├── Dockerfile
├── docker-compose.yml          ← Compose base
├── docker-compose.analang.yml  ← Compose específico Ana LangGraph
├── docker-compose.traefik.yml  ← Compose Traefik (reverse proxy)
├── MEMORY.md                   ← Memória persistente entre sessões
└── LOGS.md                     ← Logs de sessão
```

---

## Fluxo de mensagem

```
WhatsApp → Leadbox CRM → POST /webhook/leadbox
  ↓
Parser (extrair phone, texto, ticket, queue_id)
  ↓
Filtrar por tenant (123) e tipo de evento
  ↓
FinishedTicket? → reset lead (IA reativada)
QueueChange? → pausar (fila humana) ou despausar (fila IA)
  ↓
NewMessage do cliente → buffer Redis (9s delay)
  ↓ 9 segundos
processar_mensagens()
  ↓
Verificar pausa (Redis + fail-safe Supabase)
  ↓
Detectar contexto billing/manutenção (1x, salva em _context_extra)
  ↓
Buscar histórico (ana_leads.conversation_history, últimas 20)
  ↓
graph.ainvoke() → Gemini + tools (retry 3x com backoff exponencial)
  ↓
Salvar resposta no histórico (incluindo tool_calls e usage)
  ↓
Enviar via API externa Leadbox (POST com token query param)
```

> NewMessage ATIVO desde 2026-04-04. Webhook Leadbox configurado em `https://ana.fazinzz.com/webhook/leadbox`.

---

## Tools (3 ativas)

| Tool | O que faz |
|---|---|
| `consultar_cliente` | Busca no Asaas por CPF/telefone: dados, cobranças, contratos. Salva vínculo CPF/asaas_customer_id na ana_leads |
| `transferir_departamento` | POST PUSH no Leadbox com queue_id e user_id |
| `registrar_compromisso` | Registra compromisso de pagamento (data). Silencia disparos billing até a data via snooze (Supabase `billing_snooze_until` + Redis) |

IDs de transferência estão no prompt (`core/prompts.py`), não no código:
- Atendimento: queue_id=453, user_id=815 (Nathália) ou 813 (Lázaro)
- Financeiro: queue_id=454, user_id=814 (Tieli)
- Cobranças: queue_id=544, user_id=814

---

## Tabela Supabase

Uma tabela só: `ana_leads` com `conversation_history` JSONB.

Colunas usadas pela integração:
```
telefone, nome, cpf, asaas_customer_id, conversation_history,
current_state, current_queue_id, current_user_id, ticket_id,
paused_at, paused_by, responsavel, handoff_at, transfer_reason,
last_interaction_at, updated_at, billing_snooze_until
```

Tabelas Asaas compartilhadas com lazaro-real (**somente leitura** — populadas pelo sync do lazaro-real):
```
asaas_clientes, asaas_cobrancas, asaas_contratos, billing_notifications, contract_details
```
> Se dados estiverem desatualizados (ex: cliente pagou mas status ainda PENDING), o problema está no sync do lazaro-real, não neste projeto.

---

## Redis — 7 chaves

```
AGENT_ID = "ana-langgraph"

buffer:msg:ana-langgraph:{phone}       → mensagens acumuladas (TTL 300s)
lock:msg:ana-langgraph:{phone}         → impede processamento paralelo (TTL 60s)
pause:ana-langgraph:{phone}            → IA pausada (sem TTL)
context:ana-langgraph:{phone}          → contexto de mídia (TTL 300s)
snooze:billing:ana-langgraph:{phone}   → data limite do snooze billing (TTL auto)
sent:ia:ana-langgraph:{phone}          → marker anti-eco IA (TTL 15s)
dispatch:{phone}:{context}:{ref}:{date} → anti-duplicata de disparos billing/manutenção (TTL 86400s)
```

---

## Constantes (core/constants.py)

```python
TABLE_LEADS = "ana_leads"
TABLE_ASAAS_CLIENTES = "asaas_clientes"
TABLE_ASAAS_COBRANCAS = "asaas_cobrancas"
TABLE_ASAAS_CONTRATOS = "asaas_contratos"
TABLE_CONTRACT_DETAILS = "contract_details"
TENANT_ID = 123
QUEUE_IA = 537
QUEUE_BILLING = 544
QUEUE_MANUTENCAO = 545
IA_QUEUES = {537, 544, 545}  # Filas onde a IA responde
LEADBOX_API_URL, LEADBOX_API_UUID, LEADBOX_API_TOKEN  # credenciais da API
```

> **ATENÇÃO: `IA_QUEUES` inclui 3 filas.** A IA responde em 537 (fila IA), 544 (billing) e 545 (manutenção). Transferir para 544/545 NÃO pausa a IA — ela continua respondendo nessas filas. Só transferir para filas FORA de IA_QUEUES (ex: 453, 454) pausa a IA.

---

## Comandos

```bash
# Logs PM2 (últimas 50 linhas)
pm2 logs ana-langgraph --lines 50 --nostream

# Restart
pm2 restart ana-langgraph

# Health
curl http://127.0.0.1:3202/health

# Testar webhook Leadbox
curl -X POST http://127.0.0.1:3202/webhook/leadbox \
  -H "Content-Type: application/json" \
  -d '{"event":"NewMessage","tenantId":123,"message":{"body":"Oi","fromMe":false,"ticket":{"id":999,"queueId":537,"contact":{"number":"5565999990000"}}}}'

# Rodar testes (lead-simulator)
cd /var/www/ana-langgraph && source .venv/bin/activate
export $(cat .env | grep -v '^#' | grep '=' | xargs)
PYTHONPATH=/var/www/ana-langgraph python ~/.claude/skills/lead-simulator/scripts/simulate.py
```

---

## Diagnóstico de Produção

Quando pedirem para "olhar logs", "ver se deu erro", ou "verificar o que aconteceu", usar estas 3 camadas:

### Camada 1: Incidentes graves (Supabase — `ana_incidentes`)
```bash
# Últimos 20 incidentes
source .venv/bin/activate && export $(cat .env | grep -v '^#' | grep '=' | xargs)
python3 -c "
from infra.supabase import get_supabase
sb = get_supabase()
r = sb.table('ana_incidentes').select('*').order('created_at', desc=True).limit(20).execute()
for i in r.data:
    print(f\"{i['created_at'][:19]} | {i['tipo']:25} | {i['telefone']:15} | {i.get('detalhe','')[:80]}\")
"

# Filtrar por telefone específico
python3 -c "
from infra.supabase import get_supabase
sb = get_supabase()
r = sb.table('ana_incidentes').select('*').eq('telefone','PHONE_AQUI').order('created_at', desc=True).limit(10).execute()
for i in r.data: print(f\"{i['created_at'][:19]} | {i['tipo']} | {i.get('detalhe','')[:100]}\")
"
```

**23 tipos de incidente:** hallucination, gemini_falhou, resposta_vazia, consulta_falhou, transferencia_falhou, envio_falhou, mover_fila_falhou, marker_ia_falhou, buffer_erro, upsert_lead_erro, salvar_msg_erro, historico_busca_erro, historico_erro, retry_esgotado, contexto_falhou, snooze_falhou, billing_erro, manutencao_erro, media_erro, lead_reset_erro, pausa_erro, webhook_erro.

### Camada 2: Eventos operacionais (local — `logs/events.jsonl`)
```bash
# Últimos 30 eventos
tail -30 logs/events.jsonl | python3 -m json.tool

# Filtrar por telefone
grep "PHONE_AQUI" logs/events.jsonl | tail -20 | python3 -m json.tool

# Contar eventos por tipo
cat logs/events.jsonl | python3 -c "
import sys,json,collections
c=collections.Counter(json.loads(l).get('event','?') for l in sys.stdin)
for k,v in c.most_common(): print(f'{v:5} {k}')
"
```

### Camada 3: Payloads webhook raw (local — `logs/webhook_payloads.jsonl`)
```bash
# Últimos webhooks
tail -20 logs/webhook_payloads.jsonl | python3 -c "
import sys,json
for l in sys.stdin:
    d=json.loads(l); r=d.get('raw',d)
    m=r.get('message',{}) or {}; t=(m.get('ticket',{}) or {})
    c=(t.get('contact',{}) or {})
    print(f\"{d['ts'][:19]} | {r.get('event','?'):20} | {c.get('number','?')[-4:]:4} | fromMe={m.get('fromMe','-')} | sendType={m.get('sendType','-')} | q={t.get('queueId','-')}\")
"

# Filtrar fromMe de um lead específico
grep "PHONE_AQUI" logs/webhook_payloads.jsonl | python3 -c "
import sys,json
for l in sys.stdin:
    d=json.loads(l); r=d.get('raw',d); m=r.get('message',{}) or {}
    if m.get('fromMe'): print(json.dumps({'ts':d['ts'][:19],'sendType':m.get('sendType'),'userId':m.get('userId'),'body':(m.get('body') or '')[:80]},ensure_ascii=False))
"
```

### Camada 4: Logs PM2 (efêmero — rotaciona)
```bash
# Erros recentes
pm2 logs ana-langgraph --lines 100 --nostream 2>&1 | grep -i "error\|warning\|falha\|KILL\|PAUSAD"

# Filtrar por lead
pm2 logs ana-langgraph --lines 200 --nostream 2>&1 | grep "PHONE_AQUI"
```

### Alertas automáticos (WhatsApp pro admin)
- **Hallucination:** Ana disse que fez mas não chamou tool → alerta imediato
- **Gemini falhou:** 3 retries esgotados → alerta imediato
- Admin phone: variável `ADMIN_PHONE` no .env

---

## Variáveis de Ambiente

| Variável | Obrigatória | Uso |
|---|---|---|
| `GOOGLE_API_KEY` | Sim | Gemini API (LLM) |
| `SUPABASE_URL` | Sim | Banco de dados |
| `SUPABASE_KEY` | Sim | Banco de dados |
| `REDIS_URL` | Sim (default localhost) | Cache, buffer, lock, pausa |
| `LEADBOX_API_URL` | Sim | API Leadbox (envio/transferência) |
| `LEADBOX_API_UUID` | Sim | UUID do canal Leadbox |
| `LEADBOX_API_TOKEN` | Sim | Token JWT Leadbox (query param) |
| `ADMIN_PHONE` | Não | Alertas WhatsApp (desativados se vazio) |
| `AGENT_ID` | Não | Prefixo Redis (default `ana-langgraph`) |

---

## Relação com Ana original (lazaro-real)

| | Ana original | Ana LangGraph |
|---|---|---|
| **Porta** | 3115 | 3202 |
| **PM2** | `lazaro-ia` | `ana-langgraph` |
| **LLM** | Gemini direto | Gemini via LangGraph |
| **Tools** | Dict + function declaration | @tool LangChain |
| **Tabela** | `LeadboxCRM_Ana_14e6e5ce` | `ana_leads` |
| **Histórico** | `leadbox_messages_Ana_14e6e5ce` | `ana_leads.conversation_history` |
| **Canal** | Leadbox (mesmo tenant) | Leadbox (mesmo tenant 123) |

> As duas NÃO podem receber webhooks ao mesmo tempo no Leadbox.
> Para ativar a Ana LangGraph, configurar webhook do Leadbox para `https://ana.fazinzz.com/webhook/leadbox`.

---

## Pendências

### Migração para gemini-2.5-flash — deadline 1 de junho de 2026

Google desliga `gemini-2.0-flash` em 01/06/2026. Modelo configurável via `GEMINI_MODEL` em `core/grafo.py` (default `gemini-2.0-flash`).

**Regressões conhecidas do 2.5-flash** (testado 2026-04-10, 3 execuções):
- **R2**: "quero falar com o financeiro" → 2.5 responde com template billing em vez de transferir
- **R6**: "ar está fazendo barulho" em contexto manutenção → 2.5 responde com template em vez de transferir
- **X4**: "quero falar com um atendente" → 2.5 não chama tool de transferência

As 3 regressões são do mesmo padrão: o 2.5-flash ignora instrução de transferência imediata e responde com texto. Precisa ajustar prompt antes de migrar.

**Antes de migrar:**
1. Corrigir prompt para os 3 cenários acima
2. Rodar suite completa 3x com `GEMINI_MODEL=gemini-2.5-flash` e comparar com baseline 2.0-flash
3. Baseline de referência: `tests/results/all_20260410.json` (62/76 PASS com 2.0-flash)
4. Resultados do 2.5-flash: `tests/results/all_25flash_run1.json` (60/76) e `all_25flash_run2.json` (63/76)

---

## Regras

- Código enxuto — ~28 arquivos Python
- Uma tabela só (`ana_leads`) com histórico inline
- IDs de filas/usuários vivem em **2 lugares**: `core/prompts.py` E na docstring de `transferir_departamento` em `core/tools.py`. **Atualizar AMBOS** ao mudar IDs
- Sem multi-tenant — single agent, single table
- Buffer 9s — agrupa mensagens antes de processar (cap 20 msgs)
- Pausa via Redis — webhook Leadbox controla (QueueChange e FinishedTicket)
- Constantes centralizadas em `core/constants.py` — nunca hardcodar IDs
- Token Leadbox usa query param `?token=JWT`, não header Bearer
- Contexto billing/manutenção detectado 1x em processar_mensagens, não no loop ReAct

---

## Armadilhas conhecidas (ler antes de editar)

1. **`_get_supabase()` em `core/tools.py` é separada do singleton de `infra/supabase.py`.** As tools usam sua própria instância. Não confundir com `get_supabase()` de `infra/supabase.py`.
2. **`enviar_resposta_leadbox` vive em `infra/leadbox_client.py`** (não em `api/webhooks/leadbox.py`). Importar de `infra.leadbox_client`. O webhook re-importa de lá.
3. **`billing_job.py` e `manutencao_job.py` importam `enviar_resposta_leadbox` de `infra/leadbox_client.py`.** Jobs enviam mensagens pela mesma função do webhook.
4. **`_context_extra` em `grafo.py` é um dict global.** Preenchido em `processar_mensagens()`, lido em `call_model()`. Funciona porque cada chamada é por lead (sequencial via lock Redis).
5. **fromMe: NUNCA usar fila (queueId) para diferenciar IA de humano.** Usar `sendType` do payload: `"API"` = IA, qualquer outro = humano. Marker Redis é camada 1 (TTL 15s). Bug real: check `IA_QUEUES` ignorava humanos nas filas 544/545.
6. **Todos os 6 pontos que enviam para Leadbox DEVEM gravar marker Redis** (`_mark_sent_by_ia`). Se adicionar novo ponto de envio, chamar `_mark_sent_by_ia(phone)` após POST. Pontos: `enviar_resposta_leadbox`, `transferir_departamento`, fallback, alerta admin, billing, manutenção.
7. **`MAX_TOOL_ROUNDS` conta só após último HumanMessage** (desta invocação). Não contar histórico antigo — inflaria o counter e encerraria prematuramente.

---

## Regras do Asaas

- Status de contratos no banco é **UPPERCASE**: `ACTIVE`, `INACTIVE` (nunca `active`)
- Status de cobranças é **UPPERCASE**: `PENDING`, `OVERDUE`, `RECEIVED`, `CONFIRMED`
- Sempre usar os valores exatos do banco nas queries

---

## Regras do Leadbox (webhook)

- Ticket fechado: confiar apenas em `event=FinishedTicket` ou `ticket.status=closed`
- `UpdateOnTicket` com `queue_id=None` **NÃO** significa ticket fechado (é disparo genérico)
- Token usa query param `?token=JWT`, não header Bearer
- **fromMe detection (3 camadas):** (1) marker Redis `sent:ia:{agent_id}:{phone}` (TTL 15s) → IA. (2) `message.sendType == "API"` → IA (fallback se marker expirou). (3) Qualquer outro → humano → PAUSAR IA.
- **sendType valores conhecidos:** `"API"` = enviado pela API (IA), `"chat"` = enviado pelo painel (humano), `None` = ambíguo (tratar como humano)
- **Payloads capturados:** `logs/webhook_payloads.jsonl` — todos os webhooks raw, antes de qualquer filtro. Manter ativo.
