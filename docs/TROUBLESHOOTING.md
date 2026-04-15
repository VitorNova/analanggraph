# Diagnóstico de Produção

Quando pedirem para "olhar logs", "ver se deu erro", ou "verificar o que aconteceu", usar estas 4 camadas:

## Camada 1: Incidentes graves (Supabase — `ana_incidentes`)

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

**24 tipos de incidente:** hallucination, tool_como_texto, gemini_falhou, resposta_vazia, consulta_falhou, transferencia_falhou, envio_falhou, mover_fila_falhou, marker_ia_falhou, buffer_erro, upsert_lead_erro, salvar_msg_erro, historico_busca_erro, historico_erro, retry_esgotado, contexto_falhou, snooze_falhou, billing_erro, manutencao_erro, media_erro, lead_reset_erro, pausa_erro, webhook_erro.

## Camada 2: Eventos operacionais (local — `logs/events.jsonl`)

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

## Camada 3: Payloads webhook raw (local — `logs/webhook_payloads.jsonl`)

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

## Camada 4: Logs PM2 (efêmero — rotaciona)

```bash
# Erros recentes
pm2 logs ana-langgraph --lines 100 --nostream 2>&1 | grep -i "error\|warning\|falha\|KILL\|PAUSAD"

# Filtrar por lead
pm2 logs ana-langgraph --lines 200 --nostream 2>&1 | grep "PHONE_AQUI"
```

## Alertas automáticos (WhatsApp pro admin)

- **Hallucination:** Ana disse que fez mas não chamou tool → alerta imediato
- **Gemini falhou:** 3 retries esgotados → alerta imediato
- Admin phone: variável `ADMIN_PHONE` no .env
