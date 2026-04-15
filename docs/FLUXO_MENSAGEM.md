# Fluxo de mensagem

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
