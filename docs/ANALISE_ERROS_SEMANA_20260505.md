# Análise de Erros — Semana 28/04 a 05/05/2026

## Resumo executivo

7 erros reais em produção. O guardrail antierro NÃO causou nenhum deles. Os bugs reais são: extrator de resposta que descarta texto válido, histórico poluído com tool_calls não-executados, e telefone sem DDI no billing_job.

---

## ERROS IDENTIFICADOS

### ERRO 1 — Gemini quota 429 (4 clientes sem resposta)

**Quando:** 04-05/Mai

| Cliente | Input | Output |
|---|---|---|
| Sthephanny (556596425722) | "Boa tarde, gostaria de saber como funciona aluguel" | NADA (4 msgs ignoradas em 04/05) |
| Naydson (556696002103) | "Boa tarde Tudo bem?" | NADA (nunca respondeu) |
| Polly (556696670424) | "Oi boa tarde" + "Ok" | NADA (2 msgs ignoradas) |
| Maria Divina (556696899970) | mensagem perdida | NADA (falhou, respondeu depois em 05/05) |

**Root cause:** Quota `generativelanguage.googleapis.com/generate_content_free_tier_requests` zerada. O retry (3 tentativas) esgotou sem sucesso. Fallback enviado mas sem conteúdo útil.

---

### ERRO 2 — Transferência "duplicada" no histórico (3 leads)

**Sthephanny** — Input: CPF + nome para cadastro
- Output: `"Perfeito, Stefhanny"` (frase cortada) + 2x `transferir_departamento` no histórico

**Erick Andrade (556699168270)** — Input: `[Imagem enviada]`
- Output: `"Recebido"` + 2x `transferir_departamento(financeiro)` no histórico

**Eidima Gomes (556699553375)** — Input: "Fiz mudança do ar, o prazo começa de novo?"
- Output: `"Só um momento"` + 2x `transferir_departamento(atendimento)` no histórico

**Root cause:** NÃO é duplicata real. Fluxo:
1. `call_model` → Gemini retorna AIMessage com text + tool_calls
2. `route_model_output` → roteia para "tools"
3. `call_tools` → executa transferência (1x)
4. Volta para `call_model` → Gemini vê resultado e gera OUTRA AIMessage com tool_calls
5. `route_model_output` (L301-309) detecta ToolMessage anterior com "Transferido" → END
6. Tool NÃO executa 2ª vez ✅
7. **MAS** `salvar_mensagens_agente` salva TODAS as AIMessages do state — inclusive a não-executada

**Impacto real:** Zero funcional (transfer só executou 1x). Porém polui histórico e pode causar Gemini 400 em conversas futuras.

---

### ERRO 3 — `resposta_vazia` (Erick Andrade)

**Input:** `[Imagem enviada]`
**Output:** Fallback genérico em vez de "Recebido"

**Root cause:** Bug em `grafo.py` L599-613:
```python
for msg in reversed(result["messages"]):
    if isinstance(msg, AIMessage) and msg.content:
        if msg.tool_calls:
            continue  # ← PULA texto válido quando junto com tool_calls!
```

Gemini retornou `AIMessage(content="Recebido", tool_calls=[transferir])`. O extrator PULA qualquer AIMessage com tool_calls. Como AMBAS as AIMessages tinham tool_calls, `resposta = None` → fallback enviado.

---

### ERRO 4 — Snooze ignorado (DIELY DOS SANTOS)

**Input:** "Conseguirei efetuar o pagamento só no dia 8" (28/Abr)
**Output:** IA registrou snooze corretamente. Billing continuou disparando.

**Root cause:** Mismatch de telefone:
- Lead real (webhook): `telefone = "556692402027"` → `billing_snooze_until = 2026-05-08` ✅
- Lead billing_job (asaas_clientes): `telefone = "66992402027"` (sem DDI "55") → SEM snooze ❌

O `billing_job.py` cria/busca lead pelo telefone do `asaas_clientes`. O snooze é gravado no lead com DDI completo. Match nunca acontece.

Disparos que vazaram: 28/04, 30/04, 05/05.

---

### ERRO 5 — IA tratou empresa como pessoa (Nutrimais)

**Input:** "Bom Dia Ok" (contexto billing)
**Output:** `"Oi, NUTRIMAIS COMERCIAL LTDA! Tudo bem?"`

**Root cause:** `consultar_cliente` retorna `nome` do Asaas (razão social do CNPJ). O prompt não instrui a usar nome da pessoa quando disponível.

---

### ERRO 6 — IA não consultou CPF quando devia (Eidima Gomes)

**Input:** CPF `86685961287` + pergunta sobre contrato de mudança
**Output:** Transferiu direto sem consultar dados

**Root cause:** O prompt não tem regra explícita "quando cliente fornece CPF → SEMPRE chamar consultar_cliente antes de transferir". Gemini optou pelo caminho curto. O guardrail antierro não detecta esse tipo de OMISSÃO (ele detecta quando AFIRMA ter feito algo sem tool_call, não quando DEIXOU de fazer).

---

### ERRO 7 — Gemini 400 (Eidima Gomes, 29/Abr)

**Erro:** `400 Please ensure that function call turn comes immediately after a user turn or after a function response turn`

**Root cause:** Histórico corrompido. Em 05/Abr, 3 tentativas de transferência falharam com 404 (Leadbox URL inválida). Essas ToolMessages de erro ficaram no histórico. Quando Eidima voltou 24 dias depois, Gemini recebeu sequência inválida de mensagens → 400.

---

## ANÁLISE: O GUARDRAIL ANTIERRO ESTÁ CAUSANDO PROBLEMAS?

### Fluxo do guardrail (grafo.py L136-288)

```
if not response.tool_calls:              ← SÓ roda quando NÃO tem tool_calls
    checar_resposta_pre_envio()          ← Camada 1: texto afirma ter feito tool?
    if violations:
        retry com HumanMessage fake      ← Camada 2: re-invoca Gemini
        if retry.tool_calls → OK
        else contingência sintética      ← Camada 3: força tool_call
        else fallback                    ← Camada 4: msg genérica
    checar_contexto_sem_tool()           ← Camada 1b: contexto exige tool?
        retry + forçar transferência     ← Camada 2b/3b
```

### Nos 7 erros desta semana:

| Erro | Guardrail ativou? | Por quê |
|---|---|---|
| Quota 429 | ❌ | Gemini nem respondeu |
| Transfer duplicada | ❌ | Response TEM tool_calls → guardrail skipped (L136) |
| Resposta vazia | ❌ | Bug no extrator, não no guardrail |
| Snooze ignorado | ❌ | Bug de dados (telefone) |
| Empresa como pessoa | ❌ | Bug de prompt |
| Não consultou CPF | ❌ | Guardrail não detecta omissão de consultar_cliente |
| Gemini 400 | ❌ | Histórico corrompido de erro anterior |

**Veredito: O guardrail NÃO causou nenhum dos erros desta semana.**

### Mas o guardrail tem riscos latentes:

1. **Camada 2 (retry)** adiciona `HumanMessage` fake com "[SISTEMA]" ao contexto do Gemini. Se o retry TAMBÉM falhar e o histórico for salvo, essa mensagem fantasma pode confundir conversas futuras.

2. **Camada 3/3b (tool_call sintético)** força transferência baseado em inferência de texto. Se `inferir_destino_do_texto` errar o destino, transfere para fila errada.

3. **Camada 1b** assume que em contexto `manutencao` SEMPRE deve transferir. Se o cliente só disse "ok obrigado" sem querer agendamento, força transferência desnecessária.

4. **6 pontos de decisão** (guardrail 4 camadas + interceptor tool-as-text + route_model_output) tornam o debugging exponencialmente mais difícil.

### O que o guardrail resolve de fato:

Previne que a Ana diga "já transferi você" sem ter chamado a tool. Isso era um bug real e frequente no Gemini 2.0 Flash. Com o Gemini 2.5 Pro atual, esse bug praticamente não ocorre mais — nesta semana, ZERO incidentes de hallucination em produção real (os únicos são do telefone de teste `5565999990000`).

---

## PLANO DE SOLUÇÃO

### FIX 1 — Extrator de resposta (ERRO 3) — PRIORIDADE ALTA

**Arquivo:** `core/grafo.py` L599-613

**Problema:** Pula AIMessages com tool_calls, perdendo texto válido que Gemini coloca junto.

**Fix:**
```python
# ANTES (bug):
for msg in reversed(result["messages"]):
    if isinstance(msg, AIMessage) and msg.content:
        if msg.tool_calls:
            continue  # ← descarta texto válido

# DEPOIS (fix):
# Prioridade 1: AIMessage SEM tool_calls (resposta final pura)
# Prioridade 2: AIMessage COM tool_calls (texto + ação, ex: "Recebido" + transferir)
resposta = None
resposta_com_tool = None
for msg in reversed(result["messages"]):
    if isinstance(msg, AIMessage) and msg.content:
        content = msg.content
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = content.strip()
        if not content or len(content) <= 2 or not content.strip('.!?…,; \n'):
            continue
        if not msg.tool_calls:
            resposta = content
            break
        elif resposta_com_tool is None:
            resposta_com_tool = content

if not resposta and resposta_com_tool:
    resposta = resposta_com_tool
```

**Validação:** Erick teria recebido "Recebido" em vez de fallback.

---

### FIX 2 — Não salvar tool_calls não-executados (ERRO 2 + ERRO 7) — PRIORIDADE ALTA

**Arquivo:** `core/grafo.py` L577-582 (extração de `novas_mensagens`)

**Problema:** Quando `route_model_output` retorna END com a última AIMessage tendo tool_calls, essa AIMessage não-executada é salva no histórico. Polui e pode causar Gemini 400.

**Fix:**
```python
# Após extrair novas_mensagens (L578):
novas_mensagens = result["messages"][qtd_enviadas:]

# Remover última AIMessage se tem tool_calls não-executados (route retornou END)
if novas_mensagens and isinstance(novas_mensagens[-1], AIMessage):
    last_ai = novas_mensagens[-1]
    if last_ai.tool_calls:
        # Verificar se há ToolMessage correspondente após ela
        has_response = any(
            isinstance(m, ToolMessage) and m.tool_call_id in [tc.get("id") for tc in last_ai.tool_calls]
            for m in novas_mensagens[novas_mensagens.index(last_ai)+1:]
        )
        if not has_response:
            novas_mensagens = novas_mensagens[:-1]  # remover não-executada
```

**Validação:** Histórico de Erick/Sthephanny/Eidima não teria tool_calls fantasma.

---

### FIX 3 — Normalizar telefone no billing_job (ERRO 4) — PRIORIDADE ALTA

**Arquivo:** `jobs/billing_job.py`

**Problema:** `asaas_clientes` tem telefone sem DDI ("66992402027"). O webhook grava com DDI ("556692402027"). Snooze no lead com DDI não é encontrado pelo billing_job.

**Fix:** Normalizar telefone ao buscar/criar lead no billing_job:
```python
def _normalizar_telefone(phone: str) -> str:
    """Garante formato com DDI 55."""
    phone = re.sub(r'\D', '', phone)
    if len(phone) == 10 or len(phone) == 11:
        phone = "55" + phone
    return phone
```

Aplicar em `buscar_elegiveis` e `_processar_disparo` antes de criar/buscar lead.

**Validação:** Snooze de DIELY teria sido encontrado e billing silenciado até 08/05.

---

### FIX 4 — Limpar histórico corrompido (ERRO 7) — PRIORIDADE MÉDIA

**Arquivo:** `infra/nodes_supabase.py` (em `buscar_historico`)

**Problema:** ToolMessages com erro 404 ficam no histórico para sempre, causando Gemini 400 meses depois.

**Fix:** Ao carregar histórico, sanitizar sequências inválidas:
```python
def _sanitizar_historico(messages):
    """Remove tool_calls órfãos e ToolMessages de erro antigo (>7 dias)."""
    resultado = []
    for i, msg in enumerate(messages):
        if isinstance(msg, ToolMessage):
            # Pular ToolMessages com erro HTTP que são > 7 dias
            content = str(msg.content) if msg.content else ""
            if ("404" in content or "500" in content) and _msg_is_old(msg, days=7):
                continue
        resultado.append(msg)
    return resultado
```

**Validação:** Eidima não teria erro 400 ao voltar.

---

### FIX 5 — Prompt: CPF fornecido → consultar sempre (ERRO 6) — PRIORIDADE MÉDIA

**Arquivo:** `core/prompts.py`

**Problema:** Gemini não sabe que DEVE consultar quando o cliente fornece CPF.

**Fix:** Adicionar no system prompt:
```
REGRA OBRIGATÓRIA: Quando o cliente fornecer CPF (formato XXX.XXX.XXX-XX ou 11 dígitos),
SEMPRE chame consultar_cliente(cpf=...) ANTES de qualquer outra ação.
Nunca transfira sem consultar primeiro quando o CPF está disponível.
```

---

### FIX 6 — Prompt: usar nome da pessoa, não razão social (ERRO 5) — PRIORIDADE BAIXA

**Arquivo:** `core/tools.py` (retorno de `consultar_cliente`) ou `core/prompts.py`

**Fix:** No prompt, adicionar:
```
Quando o nome retornado for CAIXA ALTA ou contiver "LTDA/EIRELI/ME/SA", trate como empresa.
Use o nome do lead (da conversa) em vez da razão social para se dirigir ao cliente.
```

---

### FIX 7 — Simplificar guardrail (futuro) — PRIORIDADE BAIXA

O guardrail antierro tem 4 camadas + interceptor. Com Gemini 2.5 Pro, hallucination de tool-como-texto praticamente não ocorre mais (0 incidentes reais esta semana).

**Proposta:** Monitorar por 2 semanas. Se continuar zero incidentes:
- Manter Camada 1 (detecção) + Camada 4 (fallback)
- Remover Camada 2 (retry com HumanMessage fake) — risco de poluir contexto
- Remover Camada 3 (tool_call sintético) — risco de transferir errado
- Manter Camada 1b para manutenção (mas só como log, não como retry automático)

---

## ORDEM DE EXECUÇÃO

1. **FIX 3** (telefone billing) — impacto imediato, clientes recebendo cobrança após snooze
2. **FIX 1** (extrator resposta) — clientes recebendo fallback em vez de resposta real
3. **FIX 2** (histórico limpo) — previne Gemini 400 futuro
4. **FIX 5** (prompt CPF) — melhora qualidade de atendimento
5. **FIX 4** (sanitizar histórico) — previne erro 400 em leads antigos
6. **FIX 6** (nome empresa) — cosmético
7. **FIX 7** (simplificar guardrail) — após 2 semanas de monitoramento
