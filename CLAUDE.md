# CLAUDE.md — Ana LangGraph

> Agente Ana (Aluga-Ar) rodando em LangGraph + Gemini.

---

## Stack

- **LLM**: Google Gemini 2.0 Flash via LangGraph
- **Framework**: LangGraph (grafo ReAct)
- **API**: FastAPI (porta 3202)
- **WhatsApp**: UAZAPI (mesma instância da Ana original)
- **Banco**: Supabase (tabela `langgraph_leads`)
- **Cache/Buffer**: Redis (buffer 9s, lock, pausa)
- **CRM**: Leadbox (tenant 123, queue_ia 537)
- **Deploy**: PM2 (`ana-langgraph`)

---

## Estrutura

```
ana-langgraph/
├── api/
│   ├── app.py                  ← Entry point FastAPI
│   └── webhooks/
│       └── whatsapp.py         ← Webhook UAZAPI + parser + comandos
├── core/
│   ├── grafo.py                ← LangGraph ReAct (State, graph, processar_mensagens)
│   ├── tools.py                ← consultar_cliente + transferir_departamento
│   └── prompts.py              ← System prompt da Ana
├── infra/
│   ├── redis.py                ← RedisService (buffer, lock, pause)
│   ├── buffer.py               ← MessageBuffer (delay 9s)
│   ├── supabase.py             ← Client singleton
│   └── persistencia.py         ← Salvar histórico + enviar UAZAPI
├── tests/
│   └── cenarios.json           ← 7 cenários para lead-simulator
├── .env                        ← Credenciais
├── ecosystem.config.js         ← PM2 config
├── requirements.txt
└── Dockerfile
```

---

## Fluxo de mensagem

```
WhatsApp → UAZAPI webhook → POST /webhook/uazapi
  ↓
Parser (extrair telefone, texto, from_me)
  ↓
Comandos? (/p=pausar, /a=ativar, /r=reset) → executa e retorna
  ↓
from_me? → pausar IA (human takeover)
  ↓
Buffer Redis (RPUSH, delay 9s, cancela se nova msg)
  ↓ 9 segundos
processar_mensagens()
  ↓
Verificar pausa (Redis EXISTS pause:ana-langgraph:{phone})
  ↓
Buscar histórico (langgraph_leads.conversation_history, últimas 20)
  ↓
graph.ainvoke() → Gemini + tools
  ↓
Salvar resposta no histórico
  ↓
Enviar via UAZAPI (split 200 chars, delay 1.5s entre chunks)
```

---

## Tools (2 ativas)

| Tool | O que faz |
|---|---|
| `consultar_cliente` | Busca no Asaas por CPF: dados, cobranças, contratos |
| `transferir_departamento` | POST PUSH no Leadbox com queue_id e user_id |

IDs de transferência estão no prompt (`core/prompts.py`), não no código:
- Atendimento: queue_id=453, user_id=815 (Nathália) ou 813 (Lázaro)
- Financeiro: queue_id=454, user_id=814 (Tieli)
- Cobranças: queue_id=544, user_id=814

---

## Tabela Supabase

Uma tabela só: `langgraph_leads` com `conversation_history` JSONB.

Colunas usadas pela integração:
```
telefone, nome, conversation_history, current_state, current_queue_id,
current_user_id, ticket_id, paused_at, paused_by, responsavel,
handoff_at, transfer_reason, last_interaction_at, updated_at
```

---

## Redis — 4 chaves

```
AGENT_ID = "ana-langgraph"

buffer:msg:ana-langgraph:{phone}    → mensagens acumuladas (TTL 300s)
lock:msg:ana-langgraph:{phone}      → impede processamento paralelo (TTL 60s)
pause:ana-langgraph:{phone}         → IA pausada (sem TTL)
context:ana-langgraph:{phone}       → contexto de mídia (TTL 300s)
```

---

## Comandos

```bash
# Logs
pm2 logs ana-langgraph --lines 50 --nostream

# Restart
pm2 restart ana-langgraph

# Health
curl http://127.0.0.1:3202/health

# Testar webhook
curl -X POST http://127.0.0.1:3202/webhook/uazapi \
  -H "Content-Type: application/json" \
  -d '{"EventType":"messages","data":{"key":{"remoteJid":"5565999990000@s.whatsapp.net","fromMe":false},"message":{"conversation":"Oi"},"pushName":"Teste"}}'

# Rodar testes (lead-simulator)
cd /var/www/ana-langgraph && source .venv/bin/activate
export $(cat .env | grep -v '^#' | grep '=' | xargs)
PYTHONPATH=/var/www/ana-langgraph python ~/.claude/skills/lead-simulator/scripts/simulate.py
```

---

## Relação com Ana original (lazaro-real)

| | Ana original | Ana LangGraph |
|---|---|---|
| **Porta** | 3115 | 3202 |
| **PM2** | `lazaro-ia` | `ana-langgraph` |
| **LLM** | Gemini direto | Gemini via LangGraph |
| **Tools** | Dict + function declaration | @tool LangChain |
| **Tabela** | `LeadboxCRM_Ana_14e6e5ce` | `langgraph_leads` |
| **Histórico** | `leadbox_messages_Ana_14e6e5ce` | `langgraph_leads.conversation_history` |
| **UAZAPI** | Mesma instância | Mesma instância |
| **Leadbox** | Mesmo tenant (123) | Mesmo tenant (123) |

> As duas NÃO podem receber webhooks ao mesmo tempo.
> Para ativar a Ana LangGraph, mudar o webhook da UAZAPI para apontar para porta 3202.

---

## Skills usadas na criação

| Skill | Para que |
|---|---|
| `langgraph-whatsapp-agent` | Scaffold + templates (grafo, buffer, webhook, etc) |
| `leadbox-integration` | Integração com Leadbox (pausa/transfer) |
| `lead-simulator` | Testes E2E (7 cenários, 7/7 passando) |
| `skill-builder` | Metodologia de criação |
| `dispatch-jobs` | Jobs de disparo automático com injeção de contexto |

---

## Regras

- Código enxuto — 18 arquivos, ~1000 linhas total
- Uma tabela só (`langgraph_leads`) com histórico inline
- Tools recebem IDs direto (prompt define, não código)
- Sem multi-tenant — single agent, single table
- Buffer 9s — agrupa mensagens antes de processar
- Pausa via Redis — webhook Leadbox controla (quando implementar)

---

## TODO — Próximos passos

### 1. Plugar detecção de contexto no grafo (PRIORIDADE)

O `context_detector` já funciona (6 testes passando) mas NÃO está plugado no grafo.
Quando plugar, a IA vai saber que o lead está respondendo sobre cobrança/manutenção.

**O que fazer:**

1. Copiar `context_detector.py` da skill para o projeto:
```bash
cp ~/.claude/skills/dispatch-jobs/templates/python/context_detector.py /var/www/ana-langgraph/core/context_detector.py
```

2. Editar `core/grafo.py`, na função `call_model()`, ANTES de invocar o LLM:
```python
async def call_model(state: State) -> dict:
    # ... código existente de system_time ...

    # ADICIONAR: Detectar contexto no histórico do lead
    from core.context_detector import detect_context, build_context_prompt
    from infra.supabase import get_supabase

    supabase = get_supabase()
    extra_prompt = ""
    if supabase:
        result = supabase.table("langgraph_leads").select(
            "conversation_history"
        ).eq("telefone", state["phone"]).limit(1).execute()

        if result.data:
            history = result.data[0].get("conversation_history")
            context_type, reference_id = detect_context(history)
            if context_type:
                extra_prompt = build_context_prompt(context_type, reference_id)

    prompt = SYSTEM_PROMPT.replace("{system_time}", system_time)
    if extra_prompt:
        prompt += "\n\n" + extra_prompt

    # ... resto do código existente ...
```

3. Testar:
```bash
cd /var/www/ana-langgraph && source .venv/bin/activate
export $(cat .env | grep -v '^#' | grep '=' | xargs)
PYTHONPATH=/var/www/ana-langgraph python ~/.claude/skills/dispatch-jobs/scripts/test_context.py
```

4. Testar cenário completo com lead-simulator:
```bash
PYTHONPATH=/var/www/ana-langgraph python ~/.claude/skills/lead-simulator/scripts/simulate.py
```

### 2. Implementar job de billing

Usar skill `dispatch-jobs` para criar `jobs/billing_job.py`:
```bash
cp ~/.claude/skills/dispatch-jobs/templates/python/dispatch_job.py /var/www/ana-langgraph/jobs/billing_job.py
```
Ajustar `buscar_elegiveis()` para buscar cobranças do Asaas.
Adicionar APScheduler no `api/app.py` (ver seção 9 da skill).

### 3. Implementar webhook Leadbox

Usar skill `leadbox-integration` para criar `api/webhooks/leadbox.py`.
Necessário para pausa/despausa quando lead vai pra fila humana.

### 4. Corrigir UAZAPI 503

A instância UAZAPI retornou 503 no teste. Verificar:
```bash
curl -s "https://agoravai.uazapi.com/instance/status" -H "token: a2d9bb9c-c939-4c22-a656-7f80495681d9"
```
Se desconectada, reconectar no painel UAZAPI.

### 5. Trocar webhook da UAZAPI (QUANDO QUISER ATIVAR)

Para ativar a Ana LangGraph no lugar da Ana original:
```bash
curl -X POST "https://agoravai.uazapi.com/webhook" \
  -H "token: a2d9bb9c-c939-4c22-a656-7f80495681d9" \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"url":"https://SEU-DOMINIO/webhook/uazapi","events":["messages","connection"]}'
```

Para REVERTER para Ana original:
```bash
curl -X POST "https://agoravai.uazapi.com/webhook" \
  -H "token: a2d9bb9c-c939-4c22-a656-7f80495681d9" \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"url":"https://lazaro.fazinzz.com/webhooks/dynamic","events":["messages","connection"]}'
```
