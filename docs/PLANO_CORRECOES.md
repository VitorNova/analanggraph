# Plano de Correções — Audit Ana LangGraph

## Contexto

Audit de produção revelou 9 problemas de confiabilidade e manutenibilidade + 1 melhoria contextual.
Escopo excluído: comando /R, autenticação webhook inbound, injeção de "Oi" no billing (não injeta).

---

## A. Melhoria da mensagem seed — manutencao_job

### Situação atual
`jobs/manutencao_job.py:214-218` — quando lead novo recebe manutenção, cria histórico com:
```json
{"role": "user", "content": "Oi", "timestamp": "..."}
```
Necessário porque Gemini rejeita `model` sem `user` antes no histórico.
Billing NÃO faz isso (grava `model` direto — funciona porque Gemini aceita `model→user`).

### O que mudar
Trocar "Oi" genérico por mensagem com contexto do contrato:
```python
init_history = {"messages": [{
    "role": "user",
    "content": f"[Notificação de manutenção preventiva recebida - contrato {contract_id}]",
    "timestamp": now,
}]}
```

### Por que melhora
- Quando o lead responde, o modelo já sabe por que ele está ali desde a primeira mensagem
- Não quebra nada — `context_detector` continua achando o `context: manutencao_preventiva` na msg `model` seguinte
- Formato `[...]` indica que é mensagem de sistema, não fala real do lead

### Arquivo
- `jobs/manutencao_job.py` linha 214-218

---

## B1. 🔴 Pause Redis sem TTL — lead fica mudo pra sempre

### Situação atual
`infra/redis.py:102-106` — `pause_set` cria chave sem TTL por padrão.
Se o webhook `FinishedTicket` não chegar (bug Leadbox, timeout, servidor caiu), a pausa nunca é removida.

### O que mudar
Default TTL 24h no `pause_set`:
```python
async def pause_set(self, phone: str, ttl: Optional[int] = 86400):
    key = self._pause_key(phone)
    await self.client.set(key, "1", ex=ttl)
```

### Impacto nos callers
- `api/webhooks/leadbox.py` chama `pause_set(phone)` sem TTL → agora terá 24h (seguro: se humano continua ativo, o webhook renova a pausa; se não, expira e IA volta)
- Nenhum caller passa TTL explícito hoje, então todos ganham o default 24h

### Arquivo
- `infra/redis.py` — método `pause_set`

---

## B2. 🔴 registrar_compromisso não grava snooze no Redis

### Situação atual
`core/tools.py:453-466` — a tool grava `billing_snooze_until` no Supabase mas NÃO no Redis.
O billing job (`jobs/billing_job.py:237`) checa Redis primeiro (`redis.is_snoozed`).
Se Redis não tem snooze, vai pro fallback Supabase (~L250), que funciona MAS:
- Depende do phone format bater exatamente entre tool e billing
- Janela de inconsistência entre os dois stores

### O que mudar
Após gravar no Supabase, gravar no Redis usando pool sync que já existe em `infra/leadbox_client.py`:

```python
# Após o bloco do Supabase em registrar_compromisso:
try:
    from infra.leadbox_client import _get_sync_redis
    r = _get_sync_redis()
    days = (target - hoje).days + 1
    ttl_seconds = max(days * 86400, 86400)
    agent_id = os.environ.get("AGENT_ID", "ana-langgraph")
    r.set(f"snooze:billing:{agent_id}:{phone_clean}", data_prometida, ex=ttl_seconds)
    logger.info(f"[TOOL] Snooze Redis gravado: {phone} → {data_prometida}")
except Exception as e:
    logger.warning(f"[TOOL] Snooze Redis falhou (Supabase OK): {e}")
```

### Por que funciona
- Reutiliza `_get_sync_redis()` de `leadbox_client.py` (pool singleton, já existe)
- Tool é sync, Redis sync — sem conflito async/sync
- Se Redis falhar, Supabase já tem o dado (fallback existente no billing job continua funcionando)
- Chave e TTL seguem mesma convenção de `RedisService.snooze_set`

### Arquivos
- `core/tools.py` — função `registrar_compromisso`, após bloco Supabase

---

## B3. 🔴 _get_supabase() em tools.py cria client novo a cada call

### Situação atual
`core/tools.py:35-40`:
```python
def _get_supabase():
    return create_client(url, key)  # NOVO client toda vez
```
Cada tool call (consultar_cliente, transferir_departamento, registrar_compromisso) instancia um Supabase client novo com sessão HTTP própria. Nunca fechado. Sob carga = leak de conexões.

### O que mudar
Substituir por singleton de `infra/supabase.py`:
```python
# Remover _get_supabase() local
# Em cada tool, trocar:
#   supabase = _get_supabase()
# Por:
from infra.supabase import get_supabase
supabase = get_supabase()
```

### Risco
Import circular possível (`tools.py` ← `grafo.py` ← `supabase.py`?). Verificar antes. Se existir, mover o import para dentro da função (lazy import).

### Arquivos
- `core/tools.py` — remover `_get_supabase()`, substituir 3 chamadas (L119, L454, e qualquer outra)

---

## B4. 🟠 IDs de fila hardcoded em context_detector.py (3o lugar)

### Situação atual
CLAUDE.md documenta IDs em 2 lugares (`prompts.py` + `tools.py`). Na realidade são 3:
- `core/tools.py:258-263` — `MAPA_DESTINOS`
- `core/prompts.py` — texto do system prompt
- `core/context_detector.py:96,110` — texto do context prompt: `"fila 453, atendente 815"`

### O que mudar
Em `context_detector.py`, importar IDs de `constants.py` e interpolar:
```python
from core.constants import QUEUE_IA  # e outros necessários

# Em build_context_prompt, billing:
# Sem IDs hardcoded — billing não referencia filas no prompt

# Em build_context_prompt, manutenção:
f"transfira para Atendimento/Nathália (fila {QUEUE_ATENDIMENTO}, atendente {USER_NATHALIA})"
```

### Novos constants necessários
Adicionar em `core/constants.py`:
```python
QUEUE_ATENDIMENTO = 453
USER_NATHALIA = 815
USER_LAZARO = 813
QUEUE_FINANCEIRO = 454
USER_TIELI = 814
```

### Arquivos
- `core/constants.py` — adicionar IDs de filas/atendentes humanos
- `core/context_detector.py` — importar e interpolar
- CLAUDE.md — atualizar de "2 lugares" para "2 lugares (tools.py + context_detector.py usam constants.py)"

---

## B5. 🟠 auto_snooze busca "COBRANÇA" no texto do prompt

### Situação atual
`core/auto_snooze.py:30`:
```python
if "COBRANÇA" not in ctx_extra:
    return
```
Busca substring no texto do context prompt gerado por `context_detector.py`. Se alguém mudar o heading `## CONTEXTO ATIVO: COBRANÇA` para outra coisa, o auto-snooze para de funcionar silenciosamente (sem erro, sem log).

### O que mudar
Passar `context_type` como argumento explícito:

**Em `core/grafo.py` (~L435):**
```python
# Antes:
ctx_extra = _context_extra.get(phone, "")
await auto_snooze_billing(phone, ctx_extra, novas_mensagens, redis)

# Depois:
ctx_type = _context_type.get(phone)  # novo dict global, populado junto com _context_extra
await auto_snooze_billing(phone, ctx_type, novas_mensagens, redis)
```

**Em `core/grafo.py` (~L233), ao detectar contexto:**
```python
_context_extra[phone] = build_context_prompt(context_type, reference_id)
_context_type[phone] = context_type  # NOVO: salva tipo limpo
```

**Em `core/auto_snooze.py`:**
```python
async def auto_snooze_billing(phone: str, context_type: str, novas_mensagens: list, redis_service) -> None:
    if context_type != "billing":
        return
```

### Arquivos
- `core/grafo.py` — novo dict `_context_type`, popular ao detectar contexto, passar ao auto_snooze, limpar ao final
- `core/auto_snooze.py` — trocar assinatura e check

---

## B6. 🟠 httpx.Client sem connection pooling

### Situação atual
`infra/leadbox_client.py:85,151` e `core/tools.py:329` — criam `httpx.Client()` novo a cada request. TCP handshake toda vez para o mesmo servidor Leadbox.

### O que mudar
Singleton em `infra/leadbox_client.py`:
```python
_http_client: httpx.Client = None

def _get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(timeout=15)
    return _http_client
```

Usar em `enviar_resposta_leadbox` e `enviar_template_leadbox`. Trocar:
```python
with httpx.Client(timeout=10) as client:
    resp = client.post(...)
```
Por:
```python
client = _get_http_client()
resp = client.post(...)
```

Para `core/tools.py:329` (transferir_departamento), importar o client de leadbox_client ou criar o próprio singleton. Preferir importar.

### Arquivos
- `infra/leadbox_client.py` — adicionar `_get_http_client()`, usar nos 2 métodos de envio
- `core/tools.py` — importar client de `leadbox_client` em vez de criar novo

---

## B7. 🟠 Buffer reprocessa infinitamente em falha persistente

### Situação atual
`infra/buffer.py:106-131` — se callback falha, buffer preserva mensagens. Próxima msg do lead acumula + retenta. Se erro persiste (histórico corrompido, Gemini rejeita), loop infinito de falhas. Cap de 20 msgs eventualmente trunca.

### O que mudar
Counter de falhas por phone no Redis:
```python
# Em _process_buffered_messages, no except:
fail_key = f"buffer:fail:{AGENT_ID}:{phone}"
fail_count = await redis.client.incr(fail_key)
await redis.client.expire(fail_key, 3600)  # TTL 1h

if fail_count >= 3:
    logger.error(f"[BUFFER:{phone}] 3 falhas consecutivas — enviando fallback e limpando")
    from core.grafo import FALLBACK_MSG
    from infra.leadbox_client import enviar_resposta_leadbox
    enviar_resposta_leadbox(phone, FALLBACK_MSG)
    await redis.buffer_clear(phone)
    await redis.client.delete(fail_key)
```

No bloco de sucesso (após callback):
```python
# Zerar counter em sucesso
fail_key = f"buffer:fail:{AGENT_ID}:{phone}"
await redis.client.delete(fail_key)
```

### Arquivos
- `infra/buffer.py` — `_process_buffered_messages`, blocos try/except/success

---

## B8. 🟡 Sanitização histórico literal-match

### Situação atual
`infra/nodes_supabase.py:124`:
```python
if "transferir_departamento(" in content or "consultar_cliente(" in content or "registrar_compromisso(" in content:
    content = ""
```
Pega só formato `tool_name(`. Não pega variações como `Chamar transferir_departamento`, `[transferindo para atendimento]`, JSON inline.

### O que mudar
Usar `detectar_tool_como_texto` de `hallucination.py` que já cobre 3 formatos:
```python
from core.hallucination import detectar_tool_como_texto

# Substituir o if literal por:
if detectar_tool_como_texto(content):
    content = ""
```

### Nota
Manter o check literal como fallback rápido antes de chamar a regex (mais pesada). Ou remover por simplificação — `detectar_tool_como_texto` já cobre o caso literal.

### Arquivos
- `infra/nodes_supabase.py` — `buscar_historico`, bloco de sanitização

---

## B9. 🟡 _context_extra como dict global → State (futuro)

### Situação atual
`core/grafo.py:119` — dict global `_context_extra` preenchido em `processar_mensagens`, lido em `call_model`. Funciona porque lock Redis serializa por phone.

### O que mudar (não prioritário)
Mover para dentro do State do LangGraph:
```python
class State(TypedDict):
    messages: Annotated[list, add_messages]
    phone: str
    context_prompt: str  # NOVO
```

Não implementar agora — funciona com lock e a mudança tem blast radius alto (muda assinatura do State, afeta todos os nós).

---

## Ordem de execução

| Ordem | Item | Severidade | Arquivo principal | Estimativa |
|-------|------|-----------|-------------------|------------|
| 1 | B1 — pause TTL | 🔴 | infra/redis.py | 5 min |
| 2 | B2 — snooze Redis | 🔴 | core/tools.py | 10 min |
| 3 | B3 — singleton Supabase | 🔴 | core/tools.py | 10 min |
| 4 | A — msg seed manutenção | melhoria | jobs/manutencao_job.py | 5 min |
| 5 | B4 — centralizar IDs | 🟠 | core/context_detector.py + constants.py | 15 min |
| 6 | B5 — auto_snooze flag | 🟠 | core/auto_snooze.py + grafo.py | 10 min |
| 7 | B6 — httpx pooling | 🟠 | infra/leadbox_client.py + tools.py | 10 min |
| 8 | B7 — buffer retry limit | 🟠 | infra/buffer.py | 15 min |
| 9 | B8 — sanitização histórico | 🟡 | infra/nodes_supabase.py | 5 min |
| 10 | B9 — context global→State | 🟡 | (futuro) | — |

## Verificação

- Após B1: verificar que `pause:ana-langgraph:{phone}` tem TTL com `redis-cli TTL pause:ana-langgraph:5566...`
- Após B2: testar `registrar_compromisso` via lead-simulator, verificar chave `snooze:billing:...` no Redis
- Após B3: verificar que não há import circular com `python -c "from core.tools import TOOLS"`
- Após todos: `pm2 restart ana-langgraph` + rodar suite `tests/cenarios.json` 3x
- Commit + push após cada grupo lógico de mudanças
