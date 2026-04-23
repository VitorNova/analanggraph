# Analise Tecnica — Conversa Diego (556696894620) — 21/04/2026

Lead: DIEGO CESAR DE OLIVEIRA | CPF: 06195062103 | Telefone: 556696894620
Cobranca: pay_u7rr7losauu5l5qs | R$ 188,00 | Vencimento: 20/04/2026

---

## Timeline real da conversa

```
12:02 UTC  billing_job.py dispara cobranca (template "overdue") — salva context="billing" no historico
12:59 UTC  Cliente: "Bom dia 206 ?"
13:00 UTC  Ana: "me passa seu CPF" ← ERRADO, deveria usar buscar_por_telefone
13:00 UTC  Cliente: "06195062103"
13:00 UTC  Ana chama consultar_cliente(cpf="06195062103")
13:00 UTC  Ana: "fatura de R$ 188,00 que venceu ontem" ← mostra valor sem juros
13:02 UTC  Cliente: "Venceu ontem, hoje nao e dia util, msm assim teve juros? Subiu para 206"
13:03 UTC  Ana: "vou te passar pra equipe de cobrancas" ← anuncia antes de transferir
13:03 UTC  Ana chama transferir_departamento(destino="cobrancas")
13:03 UTC  Ana: "nosso setor de cobrancas vai te responder" ← segunda mensagem redundante
13:03 UTC  Cliente: "Ok"
13:03 UTC  Ana: "." ← resposta lixo enviada ao cliente
```

---

# P1 — CRITICO: Contexto billing perdido

O billing_job disparou e salvou `context: "billing"` no historico.
Quando o cliente respondeu 57 min depois, o contexto nao foi encontrado.
Resultado: Ana pediu CPF ao inves de usar buscar_por_telefone=true.

---

### Onde o billing_job salva o contexto no historico

```python
# jobs/billing_job.py L292-305
# _processar_disparo() salva mensagem com campo "context" no conversation_history
# ANTES de enviar o template WhatsApp. Isso garante que se o envio falhar,
# o contexto ja esta salvo para o detect_context() encontrar depois.

    history = lead.get("conversation_history") or {"messages": []}
    history["messages"].append({
        "role": "model",
        "content": message,
        "timestamp": now,
        "context": context_type,        # ← "billing" — campo que detect_context() procura
        "reference_id": reference_id,    # ← "pay_u7rr7losauu5l5qs"
    })

    supabase.table(TABLE_LEADS).update({
        "conversation_history": history,
        "updated_at": now,
    }).eq("id", lead["id"]).execute()
```

---

### Onde o detect_context() busca o campo "context"

```python
# core/context_detector.py L44-78
# Varre ultimas 10 mensagens de tras pra frente procurando campo "context".
# Se encontra e nao expirou (max 168h = 7 dias), retorna o tipo.
# NESTA CONVERSA: retornou (None, None) porque nenhuma mensagem tinha "context".

    messages = (conversation_history or {}).get("messages", [])
    if not messages:
        return None, None

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)

    for msg in reversed(messages[-10:]):    # ← varre ultimas 10
        raw_context = msg.get("context")    # ← procura campo "context"
        if not raw_context:
            continue                        # ← TODAS as msgs retornaram aqui (sem context)
```

---

### Onde processar_mensagens() usa o detect_context()

```python
# core/grafo.py L286-306
# Entry point que chama detect_context() UMA VEZ antes de invocar o grafo.
# Se encontra contexto, injeta prompt extra via _context_extra[phone].
# NESTA CONVERSA: ctx_result existia mas nenhuma msg tinha "context",
# entao _context_extra[phone] nunca foi setado.

    try:
        from core.context_detector import detect_context, build_context_prompt
        from infra.supabase import get_supabase

        supabase = get_supabase()
        if supabase:
            ctx_result = supabase.table(TABLE_LEADS).select(
                "conversation_history"
            ).eq("telefone", phone).limit(1).execute()

            if ctx_result.data:
                history_data = ctx_result.data[0].get("conversation_history")
                context_type, reference_id = detect_context(history_data)    # ← retornou (None, None)
                if context_type:                                              # ← False, nao entrou
                    _context_extra[phone] = build_context_prompt(context_type, reference_id)
```

---

### Prompt de billing que DEVERIA ter sido injetado (mas nao foi)

```python
# core/context_detector.py L95-108
# Se detect_context() tivesse retornado "billing", este prompt seria
# injetado no system_prompt. Ele contem regras criticas:
# - NAO peca CPF
# - Use buscar_por_telefone=true
# - Se ja pagou, transfira imediatamente

    if context_type == "billing":
        return f"""## CONTEXTO ATIVO: COBRANCA
O cliente recebeu disparo automatico de cobranca (ref: {reference_id or 'N/A'}).
Ele esta respondendo sobre PAGAMENTO.

REGRAS PARA ESTE CONTEXTO:
- NAO peca CPF — use a ferramenta de consulta com buscar_por_telefone=true
- Se o cliente responder com saudacao generica → ele esta respondendo a cobranca
- Se disser que ja pagou → transfira para Financeiro IMEDIATAMENTE
- O link de pagamento ja foi enviado na mensagem anterior do historico
"""
```

---

### Onde o historico pode ter sido sobrescrito (race condition)

```python
# infra/nodes_supabase.py L59-86
# salvar_mensagem() faz READ-MODIFY-WRITE no conversation_history.
# Se duas operacoes concorrentes lerem o historico ao mesmo tempo,
# a segunda sobrescreve o que a primeira salvou.
#
# Cenario provavel:
# 1. billing_job salva contexto billing no historico (12:02)
# 2. Mensagem "Era as 16:00" (20/04 21:01) chegou ANTES do billing
#    e disparou o grafo que chamou salvar_mensagens_agente()
# 3. salvar_mensagens_agente() leu historico ANTIGO (sem billing context),
#    adicionou as respostas da Ana, e salvou — sobrescrevendo o contexto billing

    def salvar_mensagem(telefone, content, direction, lead_id=None):
        existing = supabase.table(TABLE_LEADS) \
            .select("id, conversation_history") \
            .eq("telefone", telefone).limit(1).execute()    # ← READ

        history = existing.data[0].get("conversation_history") or {"messages": []}
        history["messages"].append(new_msg)                  # ← MODIFY

        supabase.table(TABLE_LEADS) \
            .update({"conversation_history": history}) \
            .eq("id", existing.data[0]["id"]).execute()      # ← WRITE (sobrescreve tudo)
```

```python
# infra/nodes_supabase.py L190-256
# salvar_mensagens_agente() tem o MESMO padrao read-modify-write.
# Se rodar concorrente com billing_job, perde o contexto billing.

    def salvar_mensagens_agente(telefone, mensagens, usage=None):
        result = supabase.table(TABLE_LEADS) \
            .select("id, conversation_history") \
            .eq("telefone", telefone).limit(1).execute()     # ← READ

        history = lead.get("conversation_history") or {"messages": []}
        # ... adiciona mensagens ...
        history["messages"].append(entry)                     # ← MODIFY

        supabase.table(TABLE_LEADS).update({
            "conversation_history": history,                   # ← WRITE (sobrescreve tudo)
        }).eq("id", lead_id).execute()
```

---

### Evidencia: historico atual NAO tem context

```
Mensagens no historico (21/04):
1. {role: "user", content: "Era as 16:00"}                    ← SEM context
2. {role: "user", content: "Bom dia 206 ?"}                   ← SEM context
3. {role: "model", content: "me passa seu CPF"}               ← SEM context
4. {role: "user", content: "06195062103"}                     ← SEM context
5. {role: "model", tool_calls: [consultar_cliente]}           ← SEM context
6. {role: "tool", content: "DADOS DO CLIENTE..."}             ← SEM context
7. {role: "model", content: "fatura de R$ 188"}               ← SEM context
...

A mensagem do billing_job com context="billing" e reference_id="pay_u7rr7losauu5l5qs"
NAO EXISTE no historico. Foi perdida.
```

---

# P2 — GRAVE: Ana respondeu apos transferencia (fila 544 nao pausa)

Ana transferiu para Cobrancas (fila 544, user 814/Tieli).
Cliente respondeu "Ok". Ana respondeu "." porque a IA nao foi pausada.

---

### Onde IA_QUEUES e definido (raiz do problema)

```python
# core/constants.py L22-26
# IA_QUEUES contem as 3 filas onde a IA responde.
# Fila 544 (Cobrancas) esta incluida porque o billing_job dispara nela.
# PROBLEMA: quando transferir_departamento move para 544 com user=814 (Tieli humana),
# a IA deveria parar, mas 544 in IA_QUEUES = True → nao pausa.

QUEUE_IA = 537
QUEUE_BILLING = 544
QUEUE_MANUTENCAO = 545
IA_QUEUES = {QUEUE_IA, QUEUE_BILLING, QUEUE_MANUTENCAO}  # ← 544 esta aqui
```

---

### Onde a transferencia acontece (POST Leadbox)

```python
# core/tools.py L266-350
# transferir_departamento() faz POST PUSH no Leadbox.
# Para destino="cobrancas", resolve queue_id=544, user_id=814.
# O POST move o ticket no CRM, e o Leadbox emite webhook QueueChange.
# A tool NAO seta pausa. Quem pausa e o webhook handler.

MAPA_DESTINOS = {
    "atendimento": (453, 815, "Nathalia (Atendimento)"),
    "financeiro": (454, 814, "Tieli (Financeiro)"),
    "cobrancas": (544, 814, "Tieli (Cobrancas)"),       # ← fila 544, user 814
    "lazaro": (453, 813, "Lazaro (Dono)"),
}

# ... dentro da tool:
    queue_id, user_id, destino_nome = MAPA_DESTINOS[destino_lower]  # (544, 814, "Tieli")

    resp = client.post(
        push_url,
        json={
            "number": telefone_limpo,
            "queueId": queue_id,       # ← 544
            "userId": user_id,         # ← 814 (Tieli, humana)
            "forceTicketToDepartment": True,
            "forceTicketToUser": True,
        },
    )
```

---

### Onde o webhook QueueChange decide pausar ou nao

```python
# api/webhooks/leadbox.py L224-285
# Quando Leadbox emite QueueChange com queue_id=544:
# L240: 544 in IA_QUEUES → True → entra no bloco de DESPAUSAR
# Resultado: IA continua ativa, mesmo com user 814 (humana) no ticket.

async def handle_queue_change(phone, queue_id, user_id, ticket_id):
    if queue_id in IA_QUEUES:                            # ← 544 in {537, 544, 545} = True
        # Fila IA → despausar
        if paused_by == "human_fromMe":
            pass  # mantem pausado se humano mandou msg
        else:
            update_data["current_state"] = "ai"          # ← DESPAUSA
            update_data["paused_at"] = None
            update_data["responsavel"] = "AI"
            await redis.pause_clear(phone)               # ← limpa pausa no Redis
    else:
        # Fila humana → PAUSAR
        update_data["current_state"] = "human"           # ← so pausa se fila NAO esta em IA_QUEUES
        await redis.pause_set(phone)
```

---

### Onde processar_mensagens() verifica pausa (e nao encontra)

```python
# core/grafo.py L210-213
# Primeira coisa que processar_mensagens() faz: checar pausa.
# Como handle_queue_change despausou (fila 544 in IA_QUEUES),
# is_paused retorna False → processa normalmente.

    if await redis.is_paused(phone):           # ← False (foi despausado)
        logger.info(f"[GRAFO:{phone}] IA pausada - ignorando")
        return                                  # ← NAO entra aqui
```

---

### Onde processar_mensagens() verifica fila no fail-safe

```python
# core/grafo.py L217-242
# Fail-safe: busca fila atual no Supabase.
# current_queue_id = 544, que esta em IA_QUEUES → permite processar.

    if _queue is not None and int(_queue) not in IA_QUEUES:  # ← 544 in IA_QUEUES = True
        logger.info(f"[GRAFO:{phone}] Fail-safe: fila {_queue} (humana) - ignorando")
        await redis.pause_set(phone)
        return                                                # ← NAO entra aqui
```

---

### Cadeia completa do P2

```
1. transferir_departamento(destino="cobrancas")     → POST queueId=544, userId=814
2. Leadbox emite QueueChange(queue_id=544)
3. handle_queue_change: 544 in IA_QUEUES → True     → DESPAUSA (errado)
4. Cliente manda "Ok"
5. processar_mensagens: is_paused? → False           → processa
6. Fail-safe: queue 544 in IA_QUEUES? → True         → permite
7. Gemini recebe "Ok" sem contexto util              → gera "."
8. enviar_resposta_leadbox(phone, ".")               → cliente recebe lixo
```

---

# P3 — MODERADO: Status PENDING em vez de OVERDUE

Cobranca venceu dia 20/04, hoje e 21/04. No Asaas esta OVERDUE,
mas na tabela asaas_cobrancas esta PENDING (sync desatualizado).

---

### Onde consultar_cliente busca cobrancas

```python
# core/tools.py L178-192
# Busca separada: OVERDUE (todas) + PENDING (so vencidas ate hoje).
# Como status no DB e PENDING (errado), a cobranca caiu no bucket PENDING.
# Resultado: rotulo "Pendente" ao inves de "VENCIDA".

    hoje_iso = date.today().isoformat()

    # Busca 1: OVERDUE (todas)
    cobrancas_overdue = supabase.table("asaas_cobrancas").select(
        "id, value, due_date, status, invoice_url"
    ).eq("customer_id", customer_id).eq(
        "status", "OVERDUE"                      # ← 0 resultados (status no DB e PENDING)
    ).is_("deleted_at", "null").order("due_date").limit(10).execute()

    # Busca 2: PENDING com due_date <= hoje
    cobrancas_pending = supabase.table("asaas_cobrancas").select(
        "id, value, due_date, status, invoice_url"
    ).eq("customer_id", customer_id).eq(
        "status", "PENDING"                      # ← encontra pay_u7rr7losauu5l5qs
    ).lte("due_date", hoje_iso                   # ← 2026-04-20 <= 2026-04-21 → True
    ).is_("deleted_at", "null").order("due_date").limit(10).execute()
```

---

### Onde o rotulo e definido

```python
# core/tools.py L206-212
# Define texto do status baseado no campo "status" do DB.
# Como status="PENDING" (DB desatualizado), mostra "Pendente" ao inves de "VENCIDA".

    for c in cobs:
        status_texto = "VENCIDA" if c["status"] == "OVERDUE" else "Pendente"
        #                                ↑ "PENDING" != "OVERDUE" → "Pendente"

        resp += f"- R$ {c['value']:.2f} | Vencimento: {c['due_date']} | {status_texto}\n"
        #            ↑ 188.00 (valor nominal, sem juros)
```

---

# P4 — MODERADO: Valor R$ 188 vs R$ 206 (sem juros)

Cliente ve R$ 206 (com juros/multa do Asaas). Ana mostrou R$ 188 (valor nominal).

---

### Onde o valor e extraido

```python
# core/tools.py L200-212
# Usa c['value'] que e o valor NOMINAL da cobranca (campo "value" na asaas_cobrancas).
# O Asaas calcula juros/multa dinamicamente e mostra no app/boleto,
# mas esse valor atualizado NAO esta na tabela sincronizada.

    resp += f"- R$ {c['value']:.2f} | Vencimento: {c['due_date']} | {status_texto}\n"
    #            ↑ 188.00 — valor original, NAO o valor com juros (206.00)

    if c.get("invoice_url"):
        resp += f"  Link do boleto/pix: {c['invoice_url']}\n"
        # ↑ link correto, mas quando cliente abre ve R$ 206
```

---

### Colunas disponiveis na asaas_cobrancas (nao tem valor atualizado)

```
Colunas relevantes:
- value: 188.0          ← valor nominal original
- invoice_url: ...      ← link que mostra valor COM juros
- (NAO existe coluna "net_value" ou "updated_value" com juros)
```

---

# P5 — MODERADO: Ana anunciou transferencia antes de executar

Ana disse "vou te passar pra equipe de cobrancas" no MESMO turno que chamou a tool.
Viola a regra 9 do prompt: "chame a ferramenta PRIMEIRO, NUNCA diga vou transferir".

---

### Regra no prompt

```python
# core/prompts.py L23
# Regra explicita: transferir silenciosamente.
# O Gemini violou gerando texto + tool_call no mesmo response.

# 9. Quando for transferir, chame a ferramenta PRIMEIRO. NUNCA diga "vou transferir",
#    "ja vou te encaminhar" ou peca confirmacao ("ok?", "pode ser?") sem efetivamente
#    chamar a tool. A acao vem antes da fala.
```

---

### Onde o hallucination detector NAO pegou

```python
# core/hallucination.py L126-167
# detectar_hallucination() so detecta quando a tool NAO foi chamada.
# Neste caso a tool FOI chamada (junto com o texto).
# O detector nao cobre o caso "texto anuncia + tool chamada no mesmo turno".

    tools_chamadas = {
        tc["name"]
        for m in novas_mensagens
        if isinstance(m, AIMessage) and m.tool_calls
        for tc in m.tool_calls
    }
    # ↑ tools_chamadas = {"transferir_departamento"} — tool PRESENTE

    for tool_name, frases in _HALL_CHECKS:
        if tool_name not in tools_chamadas and any(...):
        #  ↑ "transferir_departamento" IN tools_chamadas → skip
        #  RESULTADO: nao detecta, porque a tool foi chamada
            hallucinations.append(tool_name)
```

---

### O que o Gemini gerou (AIMessage com content + tool_calls)

```
AIMessage {
    content: "Entendi, Diego. O sistema da Asaas... vou te passar pra equipe de cobrancas..."
    tool_calls: [{
        name: "transferir_departamento",
        args: {destino: "cobrancas"}
    }]
}
```

O Gemini emitiu texto E tool_call no mesmo response. O texto anuncia a transferencia.
O grafo extraiu a resposta (L373-385) e enviou ao cliente. A tool tambem executou.
Resultado: cliente leu "vou te passar" e DEPOIS foi transferido.

---

### Onde o grafo envia a resposta sem filtrar anuncio

```python
# core/grafo.py L373-385
# Extrai ultimo AIMessage com conteudo como resposta.
# NAO filtra frases como "vou transferir", "vou te passar".
# Envia o texto bruto ao cliente.

    resposta = None
    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            content = msg.content
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            if content.strip():
                resposta = content.strip()     # ← pega o texto com "vou te passar"
                break

# core/grafo.py L506-507
# Envia ao cliente sem nenhum filtro

    if resposta:
        enviar_resposta_leadbox(phone, resposta, queue_id=current_queue, user_id=USER_IA)
```

---

# P6 — MENOR: Resposta duplicada no historico apos tool

O Gemini gerou 2 AIMessages na rodada da transferencia:
1. "vou te passar..." + tool_calls (ANTES da tool executar)
2. "nosso setor vai te responder..." (DEPOIS da tool executar)
Ambas salvas no historico. So a 2a enviada ao WhatsApp.

---

### Fluxo do grafo que gera 2 AIMessages

```python
# core/grafo.py L146-157
# O grafo ReAct tem ciclo: call_model → tools → call_model → END
#
# Ciclo nesta conversa:
# 1. call_model: Gemini gera AIMessage("vou te passar" + tool_calls=[transferir])
# 2. route_model_output: tem tool_calls → vai para "tools"
# 3. call_tools: executa transferir_departamento → ToolMessage("Transferido com sucesso")
# 4. call_model: Gemini recebe resultado da tool → gera AIMessage("nosso setor vai responder")
# 5. route_model_output: sem tool_calls → END

    builder.set_entry_point("call_model")
    builder.add_conditional_edges("call_model", route_model_output)
    builder.add_edge("tools", "call_model")    # ← apos tool, volta pro model
```

---

### Onde ambas sao salvas no historico

```python
# core/grafo.py L353-359
# novas_mensagens contem TODAS as mensagens novas (ambos AIMessages + ToolMessage).

    qtd_enviadas = len(lang_messages)
    novas_mensagens = result["messages"][qtd_enviadas:]
    mensagens_agente = [
        m for m in novas_mensagens
        if isinstance(m, (AIMessage, ToolMessage))    # ← pega AMBOS AIMessages
    ]

# core/grafo.py L498-501
# Salva TODAS as mensagens do agente no historico

    if mensagens_agente:
        salvar_mensagens_agente(phone, mensagens_agente, usage=usage or None)
```

---

# P7 — MENOR: Resposta "." nao e filtrada

Gemini retornou "." como resposta ao "Ok". Foi enviada ao cliente.

---

### Onde a resposta e validada (filtro insuficiente)

```python
# core/grafo.py L373-385
# O unico filtro e content.strip() — que retorna "." (truthy).
# NAO existe filtro para respostas triviais como ".", "..", "ok", etc.

    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            content = msg.content
            if isinstance(content, list):
                content = " ".join(...)
            if content.strip():              # ← "." passa nesse check
                resposta = content.strip()   # ← resposta = "."
                break
```

---

### Onde a resposta e enviada sem filtro de qualidade

```python
# core/grafo.py L506-507
# Envia qualquer string nao-vazia. "." foi enviada ao cliente.

    if resposta:                              # ← "." e truthy
        enviar_resposta_leadbox(phone, resposta, queue_id=current_queue, user_id=USER_IA)
        # ↑ envia "*Ana:*\n." ao WhatsApp
```

---

# Resumo: linhas criticas por arquivo

## core/constants.py
- **L26**: `IA_QUEUES = {537, 544, 545}` — 544 inclusa impede pausa apos transferencia (P2)

## core/grafo.py
- **L103-104**: `_context_extra.get(phone)` — retornou vazio porque contexto billing foi perdido (P1)
- **L210-213**: `is_paused(phone)` — retornou False porque fila 544 nao pausa (P2)
- **L237-239**: fail-safe `_queue not in IA_QUEUES` — 544 in IA_QUEUES → permitiu (P2)
- **L286-306**: `detect_context()` retornou None — contexto billing perdido (P1)
- **L353-359**: `novas_mensagens` captura AMBOS AIMessages (P6)
- **L373-385**: filtro `content.strip()` nao barra "." (P7)
- **L506-507**: envia resposta sem filtro de qualidade (P7)

## core/context_detector.py
- **L53-56**: loop `msg.get("context")` — nenhuma msg tinha context (P1)
- **L95-108**: prompt billing nunca injetado (P1)

## core/tools.py
- **L178-192**: busca OVERDUE vs PENDING separada — status errado no DB (P3)
- **L200-212**: `c['value']` mostra valor nominal sem juros (P4)
- **L258-263**: `MAPA_DESTINOS["cobrancas"] = (544, 814)` — fila que nao pausa (P2)
- **L337-338**: POST com queueId=544 (P2)

## core/prompts.py
- **L23**: regra "chame ferramenta PRIMEIRO" — Gemini violou (P5)

## core/hallucination.py
- **L164-166**: so detecta quando tool NAO foi chamada — nao cobre P5 (P5)

## api/webhooks/leadbox.py
- **L240**: `queue_id in IA_QUEUES` — 544 despausa ao inves de pausar (P2)
- **L258-264**: bloco de despausar executado para fila 544 (P2)

## infra/nodes_supabase.py
- **L59-86**: `salvar_mensagem()` read-modify-write — race condition com billing_job (P1)
- **L190-256**: `salvar_mensagens_agente()` read-modify-write — pode sobrescrever context (P1)

## jobs/billing_job.py
- **L292-305**: salva context="billing" no historico — que depois foi perdido (P1)

---

# Prioridade de correcao

| # | Gravidade | Fix estimado | Risco |
|---|-----------|-------------|-------|
| P2 | GRAVE | Medio — repensar logica de pausa vs IA_QUEUES | Alto (afeta todos os leads transferidos para 544/545) |
| P7 | MENOR | Baixo — adicionar filtro de resposta minima | Baixo |
| P1 | CRITICO | Alto — resolver race condition read-modify-write | Alto (afeta todos os disparos billing/manutencao) |
| P5 | MODERADO | Medio — melhorar prompt ou pos-processar resposta | Medio |
| P3 | MODERADO | Externo — fix no lazaro-real sync | N/A |
| P4 | MODERADO | Medio — buscar valor atualizado ou avisar no texto | Baixo |
| P6 | MENOR | Baixo — filtrar AIMessage pre-tool do envio | Baixo |
