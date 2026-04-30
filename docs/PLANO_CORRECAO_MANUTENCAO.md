# Plano de Correção — Manutenção Preventiva

**Data:** 2026-04-30
**Autor:** Vitor + Claude
**Origem:** Vídeos do Lázaro (30/04) reportando que Ana não transferia clientes de manutenção + poucos disparos sendo enviados
**Status:** Pendente implementação

---

## Contexto

O Lázaro reportou dois problemas em 30/04/2026:

1. **Ana não transferia** clientes que respondiam ao disparo de manutenção — respondía "a equipe vai entrar em contato" mas nunca chamava `transferir_departamento`. **Já corrigido** nesta sessão (commit pendente).

2. **Poucos disparos** de manutenção sendo enviados — Lázaro percebeu que a Ana está notificando muito menos clientes do que deveria.

Investigação no Supabase revelou:

| Dado | Valor |
|------|-------|
| Total de contratos | 363 |
| Com `proxima_manutencao` preenchida | 344 |
| Notificados com sucesso | 85 |
| **Com data PASSADA e NÃO notificados** | **92** |
| Sem data preenchida | 19 |
| Contrato perdido mais antigo | 14/04/2025 (1 ano!) |
| Contrato perdido mais recente | 18/04/2026 (12 dias) |

Ou seja: **92 clientes nunca receberam aviso de manutenção**.

---

## Problemas identificados (6)

### P1 — Query D-7 exato perde contratos em fins de semana e feriados

**Gravidade:** ALTA — causa raiz dos 92 perdidos

**O que acontece:**
A função `buscar_contratos_d7()` busca contratos onde `proxima_manutencao = hoje + 7` (data exata). Se o job não roda num dia (sábado, domingo, feriado, erro do PM2), o contrato com manutenção para aquele dia D-7 **nunca mais é encontrado**. A data passa e o contrato fica eternamente como `pending`.

Exemplo real:
- Contrato do KAYAN MATHEUS: `proxima_manutencao = 2026-04-18`
- D-7 seria 11/04 (sábado) → job não roda
- Domingo 12/04 → job não roda
- Segunda 13/04 → job busca `proxima_manutencao = 2026-04-20` → não encontra KAYAN (que é dia 18)
- KAYAN nunca é notificado

**Arquivo:** `jobs/manutencao_job.py`

**Linhas problemáticas:**
```python
# L48 — nome da função indica D-7 fixo
def buscar_contratos_d7(hoje: date) -> list:

# L54 — calcula data exata D-7
data_alvo = (hoje + timedelta(days=7)).isoformat()

# L61-62 — query com igualdade exata (=) em vez de range
.eq("proxima_manutencao", data_alvo)
```

**Correção proposta:**
Substituir query de data exata por **janela de 0 a 7 dias**:

```python
# ANTES (L48)
def buscar_contratos_d7(hoje: date) -> list:

# DEPOIS
def buscar_contratos_elegiveis(hoje: date) -> list:
```

```python
# ANTES (L54)
data_alvo = (hoje + timedelta(days=7)).isoformat()

# DEPOIS
data_inicio = hoje.isoformat()
data_fim = (hoje + timedelta(days=7)).isoformat()
```

```python
# ANTES (L61-62)
.eq("proxima_manutencao", data_alvo)

# DEPOIS
.gte("proxima_manutencao", data_inicio)
.lte("proxima_manutencao", data_fim)
```

**Filtro `maintenance_status` na query SQL:**
A query original não filtra `maintenance_status` no Supabase — filtra só em Python (L73). Com range `gte/lte`, a query pode retornar contratos já notificados desnecessariamente. Adicionar filtro SQL:

```python
# Adicionar após .is_("deleted_at", "null") na query
.neq("maintenance_status", "notified")
```

**Proteção contra duplicatas (3 camadas):**
1. `maintenance_status != "notified"` na query SQL → nem retorna do banco
2. `maintenance_status == "notified"` (L73) → fallback Python (defesa em profundidade)
3. Anti-duplicata Redis `dispatch:{phone}:...:{date}` (L193-195) → impede reenvio no mesmo dia

**Atualizar docstring do arquivo** (L1-12) para refletir a nova lógica: "Busca contratos com proxima_manutencao entre hoje e hoje+7".

**Atualizar chamada no `run_manutencao()`** (L156):
```python
# ANTES
elegiveis = buscar_contratos_d7(hoje)

# DEPOIS
elegiveis = buscar_contratos_elegiveis(hoje)
```

---

### P2 — Sem recuperação de contratos com data passada (92 perdidos)

**Gravidade:** ALTA — 92 clientes nunca foram notificados

**O que acontece:**
O fix P1 cobre o futuro (próximos 0-7 dias), mas não recupera os 92 contratos cuja `proxima_manutencao` já passou. Esses clientes estão com manutenção vencida e nunca receberam lembrete.

**Arquivo:** `jobs/manutencao_job.py`

**Correção proposta:**
Nova função `buscar_contratos_atrasados()` adicionada após `buscar_contratos_elegiveis()` (~L128).

```python
def buscar_contratos_atrasados(hoje: date, max_dias: int = 30, limite: int = 5) -> list:
    """Busca contratos com manutenção atrasada (data passada, não notificados).

    Recupera contratos que o job perdeu por fim de semana, feriado ou erro.
    Limita a `limite` por execução para não enviar spam.

    Args:
        hoje: Data atual.
        max_dias: Máximo de dias no passado para buscar (default 30).
        limite: Máximo de contratos atrasados por execução (default 5).
    """
```

**Lógica da query:**
```python
data_limite = (hoje - timedelta(days=max_dias)).isoformat()

supabase.table(TABLE_CONTRACT_DETAILS).select(...)
    .lt("proxima_manutencao", hoje.isoformat())           # data já passou
    .gte("proxima_manutencao", data_limite)                # até 30 dias atrás
    .not_.in_("maintenance_status", ["notified", "done"])  # não notificado
    .is_("deleted_at", "null")
    .order("proxima_manutencao", desc=True)                # mais recentes primeiro
    .limit(20)                                             # busca 20, filtra 5
```

**Deduplicação por cliente:**
O Thiago Carlos Neres tem 4 contratos. Sem dedup, receberia 4 mensagens de uma vez.

```python
# Após montar lista de elegiveis, deduplicar por customer_id
vistos = set()
resultado = []
for contrato in elegiveis:
    cid = contrato.get("customer_id")
    if cid in vistos:
        continue
    vistos.add(cid)
    resultado.append(contrato)
    if len(resultado) >= limite:
        break
```

**Cap de 5 por dia:**
O parâmetro `limite=5` garante que no máximo 5 contratos atrasados são notificados por execução. Em 18 dias úteis, os 92 perdidos seriam todos cobertos (92/5 = ~18 dias). Isso evita:
- Spam: 92 mensagens de uma vez
- Custo Meta: templates WhatsApp têm custo por envio
- Suporte sobrecarregado: Nathália receberia 92 transferências de uma vez

**Chamada no `run_manutencao()` (L154-157):**
```python
# ANTES
elegiveis = buscar_contratos_d7(hoje)
logger.info(f"[MANUTENCAO] {len(elegiveis)} contratos para notificar")

# DEPOIS
elegiveis = buscar_contratos_elegiveis(hoje)
atrasados = buscar_contratos_atrasados(hoje, max_dias=30, limite=5)
if atrasados:
    logger.info(f"[MANUTENCAO] {len(atrasados)} contratos atrasados recuperados")
    elegiveis.extend(atrasados)
logger.info(f"[MANUTENCAO] {len(elegiveis)} contratos para notificar")
```

**Context type diferenciado para atrasados:**
O template Meta (`manutencao`) diz "Está chegando a hora da manutenção preventiva" — para contratos com data vencida, o texto é enganoso. Como não podemos mudar o template Meta facilmente, aceitamos o envio (melhor avisar tarde que nunca), mas diferenciamos o `context_type` salvo no `conversation_history` para que a Ana saiba que a manutenção **já venceu** quando o cliente responder:

```python
# Em buscar_contratos_atrasados(), ao montar o item:
elegiveis.append({
    ...
    "context_type": "manutencao_atrasada",  # ← diferente de "manutencao_preventiva"
    ...
})
```

O `context_detector.py` já detecta contexto pelo campo `context` no histórico. `"manutencao_atrasada"` será tratado como manutenção (mesmo prefixo), mas permite ajustar o prompt da Ana no futuro para não dizer "está chegando" quando a data já passou.

**Proteção:** Mesma `_processar_notificacao()` existente (anti-duplicata, pausa, contexto). Nenhuma mudança nessa função — exceto P6 abaixo.

---

### P3 — `locatario_telefone` NULL (investigado, sem alteração necessária)

**Gravidade:** NENHUMA — fallback já funciona

**O que acontece:**
88 dos 92 contratos perdidos têm `locatario_telefone = NULL` na tabela `contract_details`. Porém, o job já tem fallback (L78-85) que busca `mobile_phone` na `asaas_clientes` pelo `customer_id`. Verificado: todos os 92 clientes **têm** telefone no `asaas_clientes`. Zero clientes sem telefone em nenhum lugar.

**Linhas atuais (já corretas):**
```python
# L77-85 — fallback para asaas_clientes funciona
phone = contrato.get("locatario_telefone")
if not phone:
    customer_id = contrato.get("customer_id")
    if customer_id:
        cliente = supabase.table(TABLE_ASAAS_CLIENTES).select(
            "mobile_phone"
        ).eq("id", customer_id).limit(1).execute()
        if cliente.data:
            phone = cliente.data[0].get("mobile_phone")
```

**Conclusão:** O motivo desses contratos não terem sido notificados NÃO é falta de telefone — é a query D-7 exato (P1). Nenhuma alteração necessária aqui.

---

### P4 — Equipamento "None" no template WhatsApp

**Gravidade:** MÉDIA — cliente vê "None 12000 BTUs" na mensagem

**O que acontece:**
No vídeo do Lázaro, o Thiago recebeu "Equipamento: None 12000 BTUs". O campo `marca` em `contract_details.equipamentos` (JSONB) é explicitamente `null` em vários contratos — não ausente, mas `null`.

Dados do Thiago no banco:
```json
{"btus": 12000, "marca": null, "modelo": "VG02SRINV12INT/EXT", "patrimonio": "0478"}
```

Outros contratos afetados (verificado):
- THAIS DALAVA: `marca: null`, modelo: `VG02SRINV12INT/EXT`
- EDVAN DOS SANTOS: `marca: null`, modelo: `null`
- VALDIR RODRIGUES: `marca: null`, modelo: `VG`

**Arquivo:** `jobs/manutencao_job.py`

**Linhas problemáticas:**
```python
# L94-95
eq = equipamentos[0]
equipamento_str = f"{eq.get('marca', '?')} {eq.get('btus', '?')} BTUs"
```

O `eq.get('marca', '?')` retorna `None` quando a chave existe com valor `null`.
Em Python: `{"marca": None}.get("marca", "?")` retorna `None`, NÃO `"?"`.
O `None` vira string `"None"` no f-string → cliente vê "None 12000 BTUs".

**Correção proposta:**
```python
# ANTES (L94-95)
eq = equipamentos[0]
equipamento_str = f"{eq.get('marca', '?')} {eq.get('btus', '?')} BTUs"

# DEPOIS
eq = equipamentos[0]
marca = eq.get('marca') or eq.get('modelo') or 'Split'
btus = eq.get('btus') or '?'
equipamento_str = f"{marca} {btus} BTUs"
```

**Cadeia de fallback:** `marca` → `modelo` → `"Split"` (tipo mais comum na Aluga-Ar).

**Resultado:**
- Thiago: `None 12000 BTUs` → `VG02SRINV12INT/EXT 12000 BTUs`
- Edvan (marca null, modelo null): `None 12000 BTUs` → `Split 12000 BTUs`
- Valdir: `None 12000 BTUs` → `VG 12000 BTUs`

**Mesmo fix para btus** (L95): `eq.get('btus', '?')` → `eq.get('btus') or '?'` — embora btus nunca tenha sido null nos dados, é seguro prevenir.

---

### P5 — Múltiplos contratos do mesmo cliente geram spam

**Gravidade:** BAIXA — afeta poucos clientes, mitigado pelo P2

**O que acontece:**
Thiago Carlos Neres tem 4 contratos no banco (2 notificados, 2 pendentes). Com o fix de atrasados (P2), ele poderia receber 2 notificações no mesmo dia — uma por contrato.

**Arquivo:** `jobs/manutencao_job.py`

**Correção proposta:**
Já incluída no P2 — a deduplicação por `customer_id` garante que cada cliente recebe **no máximo 1 notificação por execução**, pegando o contrato com `proxima_manutencao` mais recente.

**Nota:** Isso NÃO se aplica à `buscar_contratos_elegiveis()` (P1), porque se o cliente tem 2 contratos com manutenções em datas diferentes dentro da janela de 7 dias, faz sentido notificar ambos (são equipamentos diferentes). A dedup é só para atrasados, onde o objetivo é recuperar — não enviar tudo de uma vez.

---

### P6 — `maintenance_status = "notified"` marcado ANTES do envio (bug pré-existente)

**Gravidade:** ALTA — amplificada pelo P2

**O que acontece:**
Em `_processar_notificacao()`, o contrato é marcado como `maintenance_status = "notified"` (L256-260) **antes** de enviar o template via Leadbox (L270-276). Se o envio falha (Leadbox fora, timeout, erro Meta), o contrato fica como "notified" mas a mensagem **nunca foi entregue**. O contrato desaparece de todas as queries futuras — notificação perdida silenciosamente.

Sem o P2, isso afeta poucos contratos (só os da janela D-7 do dia). Com o P2 processando 5 atrasados/dia, o impacto de uma falha Leadbox é amplificado: 5 contratos perdidos de uma vez.

**Arquivo:** `jobs/manutencao_job.py`

**Linhas problemáticas:**
```python
# L255-260 — marca ANTES de enviar
    # Marcar contrato como notificado
    try:
        supabase.table(TABLE_CONTRACT_DETAILS).update({
            "maintenance_status": "notified",
            "notificacao_enviada_at": now,
        }).eq("id", contract_id).execute()

# L264-276 — envia DEPOIS (pode falhar, contrato já está "notified")
    if not enviar_template_leadbox(...):
        logger.error(...)
        await redis.client.set(dedup_key, "1", ex=86400)
        return False  # ← retorna False mas contrato já está "notified" no banco
```

**Correção proposta:**
Mover o update de `maintenance_status` para **depois** do envio bem-sucedido:

```python
# ANTES (L255-276): marca → envia → se falha, contrato já está "notified"

# DEPOIS: envia → se sucesso, marca. Se falha, contrato continua elegível.

    # Enviar template via Leadbox (1 POST: Leadbox → Meta → WhatsApp)
    from infra.leadbox_client import enviar_template_leadbox

    tel_envio = clean_phone if clean_phone.startswith("55") else f"55{clean_phone}"

    from core.constants import QUEUE_ATENDIMENTO, USER_NATHALIA
    if not enviar_template_leadbox(
        tel_envio, WHATSAPP_TEMPLATE, item["template_params"],
        queue_id=QUEUE_ATENDIMENTO, user_id=USER_NATHALIA,
    ):
        logger.error(f"[MANUTENCAO:{phone}] Leadbox erro ao enviar template")
        await redis.client.set(dedup_key, "1", ex=86400)
        return False

    # Marcar contrato como notificado SÓ APÓS envio bem-sucedido
    try:
        supabase.table(TABLE_CONTRACT_DETAILS).update({
            "maintenance_status": "notified",
            "notificacao_enviada_at": now,
        }).eq("id", contract_id).execute()
    except Exception as e:
        logger.warning(f"[MANUTENCAO:{phone}] Erro ao marcar contrato: {e}")
        # Não retorna False — o envio já foi feito. Anti-duplicata Redis previne reenvio hoje.

    await redis.client.set(dedup_key, "1", ex=86400)
    logger.info(f"[MANUTENCAO:{phone}] Notificação enviada (contrato={contract_id})")
    log_event("manutencao_sent", phone, contract_id=contract_id)
    return True
```

**Trade-off consciente:** Se o envio Leadbox sucede mas o update Supabase falha, o anti-duplicata Redis (TTL 24h) previne reenvio no mesmo dia. No dia seguinte, o contrato seria re-encontrado pela query e tentaria enviar de novo — o cliente receberia 2x. Isso é preferível ao cenário atual (0x). E o Redis dedup mitiga: a janela de risco é 24h, não infinita.

**Nota sobre `conversation_history`:** O contexto no histórico (L240-253) continua sendo salvo ANTES do envio. Isso é intencional — se o envio falha, o histórico registra a tentativa. Se o envio é retentado no dia seguinte, o contexto duplicado no histórico é inofensivo (o context_detector pega o mais recente).

---

## Resumo das alterações

| # | Arquivo | O que muda | Linhas afetadas |
|---|---------|-----------|-----------------|
| P1 | `jobs/manutencao_job.py` | Renomear `buscar_contratos_d7` → `buscar_contratos_elegiveis` | L48 |
| P1 | `jobs/manutencao_job.py` | Query `eq(data_alvo)` → `gte/lte` janela 0-7 dias + filtro `maintenance_status` no SQL | L54, L61-65 |
| P1 | `jobs/manutencao_job.py` | Atualizar chamada no `run_manutencao()` | L156 |
| P1 | `jobs/manutencao_job.py` | Atualizar docstring do arquivo | L1-12 |
| P2 | `jobs/manutencao_job.py` | Nova função `buscar_contratos_atrasados()` com `context_type = "manutencao_atrasada"` (~40 linhas) | Após L128 |
| P2 | `jobs/manutencao_job.py` | Chamar `buscar_contratos_atrasados()` no `run_manutencao()` | L156-157 |
| P3 | — | Nenhuma alteração (fallback já funciona) | — |
| P4 | `jobs/manutencao_job.py` | Fix `marca` null → fallback modelo → genérico | L94-95 |
| P5 | `jobs/manutencao_job.py` | Dedup por `customer_id` (incluído no P2) | Dentro de `buscar_contratos_atrasados()` |
| P6 | `jobs/manutencao_job.py` | Mover `maintenance_status = "notified"` para DEPOIS do envio Leadbox | L255-276 |

**Total:** 1 arquivo alterado (`jobs/manutencao_job.py`), ~50 linhas adicionadas, ~15 linhas reordenadas/modificadas.

**Nenhum arquivo novo.** Nenhuma mudança em tools, webhook, grafo, constants ou API.

---

## Proteções existentes (P6 corrige ordem)

| Camada | Onde | O que faz |
|--------|------|-----------|
| `maintenance_status != "notified"` | query SQL (P1/P2) | Nem retorna do banco |
| `maintenance_status == "notified"` | `buscar_contratos_*` L73 | Fallback Python (defesa em profundidade) |
| `maintenance_status = "notified"` pós-envio | `_processar_notificacao` (P6) | Só marca após Leadbox confirmar sucesso |
| Anti-duplicata Redis | `_processar_notificacao` L193-195 | Chave `dispatch:{phone}:{context}:{contract_id}:{date}` com TTL 24h |
| Pausa IA | `_processar_notificacao` L188-189 | Não envia se lead está com humano |
| Lock job | `run_manutencao` L149-150 | Impede execução paralela |
| Feriado/FDS | `run_manutencao` L136-144 | Não roda em feriado ou fim de semana |

---

## Ordem de implementação

1. **P6** (mover "notified" para depois do envio) — fix de ordem de operações, pré-requisito para P2 não amplificar perdas
2. **P4** (fix "None") — 2 linhas, risco zero
3. **P1** (janela 0-7 dias + filtro SQL) — fix principal, ~5 linhas mudadas
4. **P2** (atrasados com cap 5/dia + context_type diferenciado) — nova função, ~40 linhas
5. Testar manualmente: `python jobs/manutencao_job.py` (verificar logs — em produção envia de verdade, não é dry-run)
6. Reiniciar PM2: `pm2 restart ana-manutencao-job`

**Nota:** O passo 5 executa o job real — não existe modo dry-run. Se precisar testar sem enviar, comentar temporariamente a chamada `enviar_template_leadbox` e verificar os logs de elegíveis/atrasados encontrados.

---

## Validação pós-deploy

```sql
-- Verificar se contratos atrasados estão sendo notificados (rodar depois de 1 semana)
SELECT count(*) as ainda_pendentes
FROM contract_details
WHERE deleted_at IS NULL
  AND proxima_manutencao < CURRENT_DATE
  AND proxima_manutencao >= CURRENT_DATE - 30
  AND (maintenance_status IS NULL OR maintenance_status NOT IN ('notified', 'done'));

-- Esperado: diminuindo ~5 por dia útil
```

```bash
# Verificar logs do job
pm2 logs ana-manutencao-job --lines 50
# Procurar por: "[MANUTENCAO] X contratos atrasados recuperados"
```
