# Não Mexer — acoplamentos que quebram em vários lugares

> Lista de áreas onde uma mudança em um arquivo obriga alterar outros.
> Antes de editar, confira se o que você quer fazer está aqui — se estiver,
> pare e reavalie se o ganho compensa o custo de manter tudo em sincronia.

---

## 1. IDs de filas e usuários (queue_id / user_id)

**Vivem em 3 lugares:**
- `core/constants.py` — `TENANT_ID`, `QUEUE_IA`, `QUEUE_BILLING`, `QUEUE_MANUTENCAO`, `IA_QUEUES`
- `core/prompts.py` — regras de transferência no system prompt
- `core/tools.py` — docstring de `transferir_departamento`

**Regra:** mudou um, atualiza os 3. Nunca hardcodar ID fora desses lugares.

**IDs atuais:**
- Atendimento: queue 453, user 815 (Nathália) / 813 (Lázaro)
- Financeiro: queue 454, user 814 (Tieli)
- Cobranças: queue 544, user 814
- IA: queue 537 | Billing: 544 | Manutenção: 545

---

## 2. Pontos de envio para Leadbox (6 pontos)

Todos os 6 pontos que fazem POST para Leadbox **DEVEM** chamar `_mark_sent_by_ia(phone)` logo após o POST. Sem isso, o webhook trata a própria resposta da IA como mensagem humana e pausa.

**Pontos existentes:**
1. `enviar_resposta_leadbox` (infra/leadbox_client.py)
2. `transferir_departamento` (core/tools.py)
3. Fallback (quando Gemini falha)
4. Alerta admin
5. `jobs/billing_job.py`
6. `jobs/manutencao_job.py`

**Regra:** NUNCA criar 7º ponto de envio. Usar sempre `infra/leadbox_client.py`.

---

## 3. Client Supabase duplicado (proposital)

- `core/tools.py` → `_get_supabase()` próprio
- `infra/supabase.py` → `get_supabase()` singleton

**Regra:** NÃO tentar unificar. Tools e infra usam instâncias separadas por decisão arquitetural. Mexer quebra os dois lados ao mesmo tempo.

---

## 4. `_context_extra` global em grafo.py

Dict global preenchido em `processar_mensagens()` e lido em `call_model()`.
Só funciona porque execução é **sequencial por lead** via lock Redis.

**Regra:** NÃO paralelizar processamento, NÃO remover lock Redis, NÃO tentar mover pra state local sem antes entender o ReAct loop inteiro.

---

## 5. `IA_QUEUES = {537, 544, 545}`

3 filas onde a IA responde. Transferir para 544/545 **NÃO** pausa a IA — ela continua respondendo.

**Quem depende disso:**
- `api/webhooks/leadbox.py` (fromMe detection)
- `jobs/billing_job.py`
- `jobs/manutencao_job.py`
- `core/context_detector.py`

**Regra:** mudar esse set afeta webhook + 2 jobs + detector ao mesmo tempo. Teste os 4 antes de commitar.

---

## 6. fromMe detection — 3 camadas

Ordem obrigatória:
1. Marker Redis `sent:ia:{agent}:{phone}` (TTL 15s) → IA
2. `message.sendType == "API"` → IA (fallback se marker expirou)
3. Qualquer outro → humano → PAUSAR IA

**Regra:** NÃO simplificar pra uma só. Marker expira, sendType falha em payloads antigos. **NUNCA** usar queueId pra diferenciar IA de humano — já foi bug em produção.

---

## 7. Jobs billing/manutenção acoplados ao context_detector

`jobs/billing_job.py`, `jobs/manutencao_job.py` e `core/context_detector.py` compartilham o formato do contexto salvo em `_context_extra`.

**Regra:** mudar formato do contexto obriga atualizar: detector + 2 jobs + prompt (que referencia o contexto).

---

## 8. Separação de camadas

- `api/` → **só** recebe e roteia webhook
- `infra/` → **só** conecta (Redis, Supabase, Leadbox, retry)
- `core/` → lógica de negócio (grafo, tools, prompts, detectores)
- `jobs/` → disparos automáticos agendados

**Regra:** se você está mexendo em `api/` ou `infra/` pra resolver um bug de **comportamento** da IA, provavelmente está editando o lugar errado. O fix quase sempre é em `core/prompts.py` ou `core/grafo.py`.

---

## 9. Status Asaas em UPPERCASE

Banco guarda: `ACTIVE`, `INACTIVE`, `PENDING`, `OVERDUE`, `RECEIVED`, `CONFIRMED`.

**Regra:** nunca queriar com lowercase. Dados vêm do sync do `lazaro-real` — se estiverem desatualizados, o problema é lá, não aqui.

---

## 10. Token Leadbox = query param

API Leadbox usa `?token=JWT`, **não** header `Bearer`.

**Regra:** se copiar código de outra integração que usa Bearer, vai quebrar silenciosamente (401 ou 403).

---

## Quando quiser mexer mesmo assim

1. Releia o item correspondente acima
2. Liste todos os arquivos que vão precisar mudar juntos
3. Rode `pytest tests/test_*.py -v` antes e depois
4. Rode `tests/run_scenarios.py` se tocou em prompt, grafo, tools ou webhook
5. Registre a mudança no `MEMORY.md` pra próxima sessão não repetir
