# LOGS — Ana LangGraph

Registro de mudanças significativas no projeto.

---

## 2026-03-26 — Fase 1: Correções críticas pré-produção

### 1.1 Corrigir TENANT_ID e IDs Leadbox (45 → 123)

**Arquivos:** `api/webhooks/leadbox.py`, `core/tools.py`

**Problema:** Todos os IDs do Leadbox estavam copiados da Clara (Clínica Suprema, tenant 45).
O webhook Leadbox ia ignorar todos os eventos da Ana (tenant 123), e a tool de transferência
ia mandar leads para filas erradas.

**O que mudou:**
- `leadbox.py`: TENANT_ID 45→123, QUEUE_IA 562→537, removido USER_BOT (não existe na Ana)
- `tools.py`: IDs de filas (453/454/544 em vez de 127), bloqueio 562→537, UUID/token agora lidos de env vars (`LEADBOX_API_UUID`, `LEADBOX_API_TOKEN`) em vez de hardcoded da Clara
- `tools.py`: Removido header Authorization duplicado (token já vai na URL, pattern comprovado em produção)
- `tools.py`: Adicionada validação early-return se credenciais Leadbox estiverem vazias

### 1.2 Validação de histórico (remover órfãs de ToolMessage)

**Arquivo:** `infra/persistencia.py` → `buscar_historico()`

**Problema:** Sem validação, um corte de histórico (últimas 20 msgs) podia deixar ToolMessage
sem AIMessage precedente. O Gemini rejeita esse formato e crashava.

**O que mudou:**
- Reconstrução de AIMessage com `tool_calls` quando salvos no histórico JSONB
- Reconstrução de ToolMessage com `name` e `tool_call_id`
- Validação completa: remove ToolMessage órfãs, blocos incompletos de tool_calls, e sequências quebradas
- Mesmo algoritmo validado em produção na Clara (`agente-langgraph`)

### 1.3 Salvar ToolMessage + AIMessage completa no histórico

**Arquivos:** `infra/persistencia.py`, `core/grafo.py`

**Problema:** Só a resposta final (texto) era salva. Tool calls e seus resultados eram perdidos.
Na próxima conversa, a IA não sabia o que já tinha consultado e repetia consultas.

**O que mudou:**
- Nova função `salvar_mensagens_agente()` em `persistencia.py`: salva AIMessage (com `tool_calls` e `token_count`) + ToolMessage (com `tool_name` e `tool_call_id`)
- `grafo.py` refatorado: extrai mensagens novas do agente após `graph.ainvoke()`, usa `salvar_mensagens_agente()` em vez de `salvar_mensagem(outgoing)`
- Extrai `usage_metadata` (input/output/total tokens) da última AIMessage

### Review (/review)

4 issues encontradas, 2 auto-corrigidas:
- **[FIXED]** `tools.py:136` — Credenciais Leadbox vazias sem validação → early-return adicionado
- **[FIXED]** `tools.py:145` — Token duplicado URL+header → removido header, mantido ?token= na URL
- **[INFO]** `persistencia.py:152` — tool_calls sem "id" podem criar set vazio (consistente com referência)
- **[INFO]** `grafo.py:156` — `os.environ["_CURRENT_PHONE"]` é race condition teórica (fix real é InjectedState na Fase 2)

---

## 2026-03-27 — Fase 2: Robustez

### 2.1 InjectedState no phone (transferir_departamento)

**Arquivos:** `core/tools.py`, `core/grafo.py`

**Problema:** O telefone era passado via `os.environ["_CURRENT_PHONE"]` (variável global), race condition
se dois leads processados simultaneamente. O LLM também precisava "lembrar" o telefone para passar como parâmetro.

**O que mudou:**
- `tools.py`: `transferir_departamento` agora usa `Annotated[str, InjectedState("phone")]` para receber o phone automaticamente do state do grafo
- `tools.py`: Removido parâmetro `telefone` explícito, LLM só precisa passar `queue_id` e `user_id`
- `grafo.py`: Removido `os.environ["_CURRENT_PHONE"]`

### 2.2 Notificação admin em erro

**Arquivo:** `core/grafo.py`

**Problema:** Erros no `graph.ainvoke()` eram silenciosos. Ninguém sabia quando a IA falhava.

**O que mudou:**
- Nova função `_notificar_erro()`: log JSON estruturado + WhatsApp para `ADMIN_PHONE`
- Constante `FALLBACK_MSG` centralizada (antes era string duplicada)
- Quando todas as tentativas falham, envia fallback pro lead E notifica o admin

### 2.3 Plugar context_detector no grafo

**Arquivos:** `core/context_detector.py` (novo), `core/grafo.py`

**Problema:** O context_detector existia como template na skill dispatch-jobs mas não estava integrado.
Sem ele, a IA não sabia se o lead estava respondendo sobre cobrança ou manutenção.

**O que mudou:**
- Copiado `context_detector.py` para `core/`
- `call_model()` agora busca `conversation_history` do Supabase antes de invocar o LLM
- Se detectar contexto (billing/manutenção), injeta prompt extra no system_prompt
- Erro na detecção é tratado graciosamente (warning, não crash)

### 2.4 Prefixo "*Ana:*" no primeiro chunk

**Arquivos:** `infra/persistencia.py`, `core/grafo.py`

**Problema:** O lead não sabia quem estava respondendo (IA ou humano).

**O que mudou:**
- `enviar_resposta()` agora aceita `agent_name` opcional
- Se informado, prefixa `*Ana:*\n` no primeiro chunk (bold no WhatsApp)
- `processar_mensagens()` chama `enviar_resposta(phone, resposta, agent_name="Ana")`

---

## 2026-03-27 — Fase 3: Funcionalidades

### 3.1 Suporte multimodal (imagem/áudio/documento)

**Arquivos:** `core/whatsapp/baixar_midia.py` (novo), `api/webhooks/whatsapp.py`, `core/grafo.py`

**O que mudou:**
- Novo módulo `core/whatsapp/baixar_midia.py`: funções `baixar_imagem`, `baixar_audio`, `baixar_documento` via UAZAPI `/message/download`
- Parser do webhook atualizado: detecta `imageMessage`, `audioMessage`, `documentMessage` e extrai base64
- `processar_mensagens()` constrói `HumanMessage` multimodal com content list (text + image_url/media) igual à Clara
- Texto da mensagem é salvo no histórico, base64 vai direto pro grafo (não salva no Supabase)

### 3.2 Jobs/rotinas (leads abandonados)

**Arquivos:** `jobs/rotinas.py` (novo), `ecosystem.config.js`

**O que mudou:**
- `marcar_perdidos()`: marca leads em estado "ai" sem interação há 7 dias como "abandoned"
- PM2 cron: `ana-pipeline-perdidos` roda à meia-noite diariamente

### 3.3 Billing job (disparos de cobrança)

**Arquivos:** `jobs/billing_job.py` (novo), `ecosystem.config.js`

**O que mudou:**
- `buscar_elegiveis()`: busca cobranças PENDING/OVERDUE no Supabase com join de clientes
- Régua de dias úteis: `[-1, 0, 1, 3, 5, 7, 10, 15]` com 5 templates (reminder, due_date, overdue1/2/3)
- `_processar_disparo()`: anti-duplicata Redis (24h), salva contexto ANTES de enviar (ordem crítica), UAZAPI send
- Lock Redis para impedir execução paralela
- PM2 cron: `ana-billing-job` roda seg-sex às 9h
- Context detector já plugado na Fase 2 vai detectar "billing" no histórico quando o lead responder
