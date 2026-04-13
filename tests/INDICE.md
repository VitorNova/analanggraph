# Indice de Testes — Ana LangGraph

## 1. Cenarios E2E (lead-simulator)

| Arquivo | Cenarios | Foco |
|---|---|---|
| `cenarios.json` | 30 | Suite completa: saudacao, billing, manutencao, snooze, defeito, transferencia |

```bash
# Via lead-simulator
PYTHONPATH=. .venv/bin/python3 ~/.claude/skills/lead-simulator/scripts/simulate.py \
  --cenarios-file tests/cenarios.json

# Cenario especifico
PYTHONPATH=. .venv/bin/python3 ~/.claude/skills/lead-simulator/scripts/simulate.py \
  --cenarios-file tests/cenarios.json --cenario "saudacao"
```

## 2. Testes Unitarios (pytest)

| Arquivo | Foco |
|---|---|
| `test_context_detector.py` | Deteccao billing/manutencao no historico |
| `test_fromme_detection.py` | 3 camadas: marker Redis → sendType → humano |
| `test_hallucination.py` | Deteccao pos-resposta (texto vs tools) |
| `test_interceptor.py` | Interceptor de tool calls |
| `test_interceptor_real.py` | Interceptor com grafo real |
| `test_leadbox_client.py` | Client HTTP Leadbox |
| `test_retry.py` | Logica de retry |
| `test_tool_como_texto.py` | Gemini escreve tool como texto |
| `test_user_attribution.py` | Atribuicao de usuario no ticket |

```bash
PYTHONPATH=. .venv/bin/pytest tests/ -v
```

## 3. Runners E2E (scripts)

| Arquivo | Foco |
|---|---|
| `run_scenarios.py` | Roda todos os cenarios contra grafo real |
| `run_billing_scenarios.py` | Cenarios de cobranca |
| `run_bug_original.py` | Bug de limpeza de ar |
| `run_tool_calls.py` | Validacao de tool calls |

## 4. Baselines e resultados

| Arquivo | O que contem |
|---|---|
| `results/all_20260410.json` | Baseline 2.0-flash: 62/76 PASS |
| `results/all_25flash_run1.json` | 2.5-flash run1: 60/76 |
| `results/all_25flash_run2.json` | 2.5-flash run2: 63/76 |
| `results/tool_text_20260410.json` | Resultados tool-como-texto |
| `flows/bug_original_limpeza_ar.json` | Flow do bug original |
| `flows/tool_calls_esperados.json` | Tool calls esperados por cenario |

## 5. Reports

| Arquivo | O que contem |
|---|---|
| `report.json` | Ultimo report do lead-simulator |
| `relatorio_correcao_tool_calling.md` | Analise de correcao de tool calling |
