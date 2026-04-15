# Diff — Fluxo de Manutenção Preventiva (produção vs clone-ana)

Comparação entre o código de produção (`/var/www/ana-langgraph`) e o clone-ana
(https://github.com/VitorNova/clone-ana — `jobs/manutencao_job.py`).

## Arquivos do projeto em produção com lógica de manutenção

Código executável (fluxo de disparo / contexto / resposta):

1. `jobs/manutencao_job.py` — job D-7 (único arquivo presente no clone-ana para comparação direta)
2. `core/context_detector.py` — detecta contexto `manutencao_preventiva` no histórico para injetar no prompt
3. `core/grafo.py` — aplica `_context_extra` quando contexto manutenção é detectado
4. `core/prompts.py` — regras do system prompt sobre manutenção (fora de área, transferência etc.)
5. `core/tools.py` — `transferir_departamento` usa fila manutenção em alguns cenários
6. `infra/incidentes.py` — tipo `manutencao_erro`
7. `ecosystem.config.js` — processo PM2 `ana-manutencao-job` (cron `0 9 * * 1-5`)

Documentação / testes (não comparados aqui, só fluxo executável): `CLAUDE.md`,
`docs/FLUXO_MENSAGEM.md`, `docs/TROUBLESHOOTING.md`, `docs/NAO_MEXER.md`,
`tests/test_context_detector.py`, `tests/cenarios.json`, suites em `tests/results/`.

> O repositório clone-ana foi consultado apenas via a URL fornecida
> (`jobs/manutencao_job.py`). As diferenças a seguir são todas deste arquivo.
> Os demais arquivos de produção listados acima não têm contraparte buscada no
> clone-ana neste diff.

---

### Comentários explicativos em português com marcadores "validado/não validado"

**Arquivo em produção:** `jobs/manutencao_job.py` (sem esses comentários)
**Arquivo no clone-ana:** `jobs/manutencao_job.py` (linhas 1, 15, 27, 31, 38, 49, 126, 164, 184, 206, 227, 251, 268)

**Produção:**
```python
"""Job de Manutenção Preventiva — Lembrete D-7.
...
"""

import asyncio
...
```

**Clone-ana:**
```python
# Descrição do que este arquivo faz — linha 2 até 13 (boilerplate, não requer validação)
"""Job de Manutenção Preventiva — Lembrete D-7.
...
"""

# Importa as bibliotecas necessárias — linha 16 até 25 (boilerplate, não requer validação)
import asyncio
...
# Importa conexões do projeto (banco de dados, constantes) — linha 28 até 29 (boilerplate, não requer validação)
...
# Configura o sistema de logs — linha 32 até 36 (boilerplate, não requer validação)
...
# Mensagem que o cliente recebe no WhatsApp — linha 39 até 46 (não validado)
...
# Busca contratos com manutenção em 7 dias e monta a mensagem — linha 50 até 123 (query, filtro status, fallback telefone: validados | montagem mensagem: não validado)
...
# Função principal que roda o job de manutenção — linha 127 até 161 (não validado)
...
# Processa e envia uma notificação para cada cliente — linha 170 até 271 (validado)
...
    # Salvar contexto ANTES de enviar (validado)
...
    # Lead novo: inicializar histórico com role:user para coerência (validado)
...
    # Salvar contexto role:model no histórico (validado)
...
    # Enviar via Leadbox (validado)
...
# Executa o job quando o arquivo é rodado diretamente — linha 269 até 270 (boilerplate, não requer validação)
```

**O que mudou:** clone-ana adiciona comentários/selos "validado / não validado /
boilerplate" em cada bloco do arquivo. Produção não tem nenhum desses selos.

---

### Checagem de fim de semana no `run_manutencao`

**Arquivo em produção:** `jobs/manutencao_job.py` (linhas 124-128)
**Arquivo no clone-ana:** `jobs/manutencao_job.py` (linhas 131-132)

**Produção:**
```python
hoje = date.today()
weekday = hoje.weekday()
if weekday >= 5:
    logger.info("[MANUTENCAO] Fim de semana, pulando")
    return

redis = await get_redis_service()
```

**Clone-ana:**
```python
hoje = date.today()
redis = await get_redis_service()
```

**O que mudou:** clone-ana **removeu** a guarda de fim de semana. Em produção o
job aborta se rodar no sábado/domingo; no clone-ana ele roda em qualquer dia
(passa a depender só do cron PM2 `0 9 * * 1-5` para não rodar no fim de semana).

---

### Criação de lead novo — inicialização do `conversation_history`

**Arquivo em produção:** `jobs/manutencao_job.py` (linhas 199-208)
**Arquivo no clone-ana:** `jobs/manutencao_job.py` (linhas 202-221)

**Produção:**
```python
if not lead:
    from infra.nodes_supabase import upsert_lead
    lead_id = upsert_lead(clean_phone)
    if lead_id:
        result = supabase.table(TABLE_LEADS).select(
            "id, conversation_history"
        ).eq("id", lead_id).limit(1).execute()
        if result.data:
            lead = result.data[0]
```

**Clone-ana:**
```python
if not lead:
    from infra.nodes_supabase import upsert_lead
    lead_id = upsert_lead(clean_phone)
    if lead_id:
        # Lead novo: inicializar histórico com role:user para coerência (validado)
        init_history = {"messages": [{
            "role": "user",
            "content": "Oi",
            "timestamp": now,
        }]}
        supabase.table(TABLE_LEADS).update({
            "conversation_history": init_history,
            "updated_at": now,
        }).eq("id", lead_id).execute()

        result = supabase.table(TABLE_LEADS).select(
            "id, conversation_history"
        ).eq("id", lead_id).limit(1).execute()
        if result.data:
            lead = result.data[0]
```

**O que mudou:** clone-ana, ao criar um lead inexistente, **pré-popula** o
`conversation_history` com uma mensagem sintética `{"role": "user", "content":
"Oi"}` antes de adicionar a mensagem `role: "model"` do disparo. Produção
apenas re-lê o lead recém-criado e depois anexa direto a mensagem `model`.
Efeito: no clone-ana o primeiro item do histórico é sempre `user`; em produção
o primeiro item pode ser `model` (a própria notificação D-7).

---

## Resumo

**3 diferenças encontradas em 1 arquivo** (`jobs/manutencao_job.py`):

1. Comentários/selos "validado / não validado / boilerplate" adicionados em todo o arquivo no clone-ana.
2. Guarda de fim de semana (`weekday >= 5`) presente em produção e **removida** no clone-ana.
3. Inicialização do `conversation_history` com mensagem sintética `role:user "Oi"` para leads novos — presente no clone-ana, ausente em produção.

Outros arquivos de produção com lógica de manutenção (`core/context_detector.py`,
`core/grafo.py`, `core/prompts.py`, `core/tools.py`, `infra/incidentes.py`,
`ecosystem.config.js`) não foram comparados — o clone-ana consultado via a URL
fornecida contém apenas `jobs/manutencao_job.py`.
