# Governance — Regras de organização do projeto

## Arrumação pós-sessão

Ao final de cada sessão que modificou código ou resolveu bug, execute este checklist:

### 1. MEMORY.md — registrar o que foi feito
- Adicionar entrada no formato telegráfico (máx 3 linhas por item):
  ```
  ## [DD/MM/AAAA] Título curto
  - O que quebrou → o que foi feito + arquivo modificado
  ```
- NUNCA escrever narrativa, investigação ou fontes de pesquisa — isso é git log
- Se resolveu uma pendência, marcar `[x]` na seção Pendências

### 2. CLAUDE.md — manter atualizado se houve mudança estrutural
- Nova tool adicionada/removida → atualizar tabela "Tools"
- Novo arquivo crítico → atualizar "Estrutura"
- NÃO adicionar bugs resolvidos aqui — isso vai no MEMORY.md

### 3. tests/INDICE.md — registrar testes novos
- Novo `test_*.py` ou `run_*.py` → adicionar na tabela correspondente
- Novo baseline em `tests/results/` → registrar com modelo e score

### 4. Limpeza
- Arquivos temporários (scripts inline, dumps, logs copiados) → deletar
- NÃO deixar MDs soltos na raiz — se for referência, vai em `docs/`

### 5. Auto-memory (quando aplicável)
- Feedback do usuário (correção, preferência) → salvar em `/root/.claude/projects/.../memory/`
- Decisão arquitetural importante → salvar como project memory
- NÃO salvar coisas que o git log já tem

---

## Onde colocar cada coisa

| O que voce criou | Onde vai | Naming / Regra |
|------------------|----------|----------------|
| Teste unitario (pytest) | `tests/test_*.py` | `test_{modulo_testado}.py` |
| Cenario E2E novo | Adicionar em `tests/cenarios.json` | Arquivo unico — NAO criar cenarios_*.json separados |
| Runner E2E customizado | `tests/run_*.py` | `run_{tema}.py` |
| Baseline de resultado | `tests/results/` | Nomeado com data: `all_YYYYMMDD.json` |
| Documentacao tecnica | `docs/` | Nunca na raiz |
| Script utilitario | `scripts/` | Descartavel apos uso → deletar |
| Constante/ID novo | `core/constants.py` | Nunca hardcodar em outro arquivo |
| Nova tool do LLM | `core/tools.py` | Na lista TOOLS do mesmo arquivo |
| Regra de negocio / prompt | `core/prompts.py` | Nunca em `api/` ou `infra/` |
| Novo handler webhook | `api/webhooks/leadbox.py` | Unico ponto de entrada webhook |
| Novo tipo de incidente | `infra/incidentes.py` | Usar `registrar_incidente()` |
| Novo job automatico | `jobs/{nome}_job.py` | Registrar no PM2 ecosystem |
| Logica de deteccao | `core/hallucination.py` | Pos-resposta: texto vs tools |
| Contexto de disparo | `core/context_detector.py` | billing ou manutencao |
| Logica de snooze | `core/auto_snooze.py` | Fallback 48h se Gemini nao chamar tool |
| Logica de buffer | `infra/buffer.py` | Cap 20 msgs, delay 9s |
| Logica de Redis | `infra/redis.py` | Locks, pausa, markers, snooze |
| Logica de Supabase | `infra/supabase.py` + `infra/nodes_supabase.py` | supabase.py = client, nodes = historico/persistencia |
| Envio para Leadbox | `infra/leadbox_client.py` | Unico ponto de envio — NAO criar outro |
| Retry/resilencia | `infra/retry.py` | Retry exponencial com backoff |

## Proibido

- **NUNCA criar .md na raiz** — doc vai em `docs/`, memoria vai no MEMORY.md
- **NUNCA criar script inline** para teste — usar `simulate.py` oficial ou pytest
- **NUNCA criar arquivo em `core/`** sem necessidade real — 7 arquivos, manter enxuto
- **NUNCA deixar report/dump solto** — baselines em `tests/results/`, deletar o resto
- **NUNCA hardcodar ID** de fila, tenant, usuario — tudo em `core/constants.py`
- **NUNCA colocar logica de negocio em `api/` ou `infra/`** — `api/` so recebe e roteia, `infra/` so conecta
- **NUNCA criar novo ponto de envio para Leadbox** — usar `infra/leadbox_client.py` + chamar `_mark_sent_by_ia`
- **NUNCA duplicar client Supabase** — tools usam `_get_supabase()` propria, infra usa `infra/supabase.py`

## Antes de criar qualquer arquivo

1. Perguntar: ja existe um lugar para isso? (provavelmente sim)
2. Se for cenario E2E: adicionar em `tests/cenarios.json` (NAO criar arquivo novo)
3. Se for teste unitario: `tests/test_{modulo}.py`
4. Se for doc: vai em `docs/`
5. Se for temporario: deletar quando terminar
6. Registrar em `tests/INDICE.md` se criou teste novo
