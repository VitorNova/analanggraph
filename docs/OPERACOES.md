# Comandos e Operações

## Básicos

```bash
# Logs PM2 (últimas 50 linhas)
pm2 logs ana-langgraph --lines 50 --nostream

# Restart
pm2 restart ana-langgraph

# Health
curl http://127.0.0.1:3202/health

# Testar webhook Leadbox
curl -X POST http://127.0.0.1:3202/webhook/leadbox \
  -H "Content-Type: application/json" \
  -d '{"event":"NewMessage","tenantId":123,"message":{"body":"Oi","fromMe":false,"ticket":{"id":999,"queueId":537,"contact":{"number":"5565999990000"}}}}'
```

## Testes unitários

```bash
cd /var/www/ana-langgraph && source .venv/bin/activate
PYTHONPATH=. pytest tests/test_*.py -v
```

## Testes E2E (requer .env, Redis, Supabase, Gemini)

```bash
export $(cat .env | grep -v '^#' | grep '=' | xargs)
PYTHONPATH=. python tests/run_scenarios.py
```

## Lead-simulator (skill Claude)

```bash
PYTHONPATH=/var/www/ana-langgraph python ~/.claude/skills/lead-simulator/scripts/simulate.py
```

## Diagnóstico de eventos

```bash
python scripts/resumo.py              # resumo geral
python scripts/resumo.py --last 1h    # última hora
python scripts/resumo.py --errors     # só erros
```
