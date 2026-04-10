# Relatório de Correção — Bug Tool-as-Text

Data: 2026-04-10
Bug: Gemini 2.0 Flash escreve nome da tool como texto (`transferir_departamento(queue_id=453, user_id=815)`) em vez de usar function calling nativo.
Lead afetado: 556699198912 — 5 ocorrências em 20 mensagens.

---

## Ações Implementadas

| # | Ação | Arquivo(s) |
|---|------|-----------|
| 3 | Limpar prompt — remover sintaxe literal de tool calls | `core/prompts.py`, `core/context_detector.py` |
| 4 | Hallucination bloqueante — interceptor que bloqueia tool-as-text antes do envio | `core/grafo.py`, `core/hallucination.py` |

---

## O que existe em disco vs o que não existe

### Arquivos de resultado procurados

| Local | Encontrado? | Conteúdo |
|-------|-------------|----------|
| `.claude/skills/lead-simulator/results/report_20260410_*.json` | **Não existe** | Testes rodaram sem flag `--report`, nenhum JSON foi gerado |
| `tests/report.json` | Existe mas de 09/04 | Não foi sobrescrito pelos testes de hoje |
| `.claude/skills/test-flow/flows/2026-04-10.json` | **Não existe** | Não usei o test-flow, usei o simulate.py |
| `logs/events.jsonl` | Vazio | Sem dados |
| PM2 logs | Não aplicável | Os testes rodam via simulate.py (processo separado), não pelo PM2 |

### O que o simulate.py salva vs o que descarta

O simulate.py (linhas 1123-1139) extrai de cada AIMessage:
- `tc["name"]` e `tc["args"]` dos `msg.tool_calls` → salva em `result.tool_calls`
- `msg.content` → salva em `result.resposta` (só o último AIMessage com texto)

**O que ele NÃO salva:**
- O objeto `AIMessage` bruto completo
- As `ToolMessage` (role: tool) — descartadas após extração
- O histórico completo de mensagens do grafo
- Nenhum log é escrito em arquivo (só stdout)

### Conclusão sobre evidências

**Não tenho o AIMessage bruto, não tenho as ToolMessages, não tenho log em arquivo.** A única evidência que existiu foi o stdout do terminal no momento da execução, que está nos tool results desta conversa mas não em disco.

---

## Testes Executados — Output real do stdout

Abaixo está o output **exato** que o simulate.py imprimiu no terminal. Isso é tudo que tenho.

### Execução 1: Grupo `tool_text` (8 cenários)

Comando: `PYTHONPATH=/var/www/ana-langgraph python3 .claude/skills/lead-simulator/scripts/simulate.py --group tool_text`

```
[TT1] Tool-as-text — limpeza de ar → transfere (não escreve tool como texto)
  Mensagem: "Tenho um ar condicionado em casa preciso fazer uma limpeza ar de 12 mil btus inverter"
  Tool calls: nenhum
  Resposta: Para agendar a limpeza do seu ar-condicionado, preciso transferir você para o Atendimento/Nathália (fila 453, atendente 815).
  Validações:
    ✅ Nenhuma tool chamada (permitidas: ['transferir_departamento'])
    ✅ Resposta NÃO contém 'transferir_departamento'
    ✅ Resposta NÃO contém 'queue_id'
    ✅ Resposta NÃO contém 'user_id'
    ✅ Resposta NÃO contém 'consultar_cliente'
    ✅ Resposta NÃO contém 'registrar_compromisso'
  Tempo: 1743ms
  Resultado: ✅ PASS

[TT2] Tool-as-text — ar pingando → transfere (não escreve tool como texto)
  Mensagem: "Meu ar tá pingando água, o que faço?"
  Tool call: transferir_departamento(queue_id="453", user_id="815")
  Resposta: 
  Validações:
    ✅ Nenhuma tool chamada (permitidas: ['transferir_departamento'])
    ✅ Resposta NÃO contém 'transferir_departamento'
    ✅ Resposta NÃO contém 'queue_id'
    ✅ Resposta NÃO contém 'user_id'
    ✅ Resposta NÃO contém 'consultar_cliente'
    ✅ Resposta NÃO contém 'registrar_compromisso'
  Tempo: 1369ms
  Resultado: ✅ PASS

[TT3] Tool-as-text — cancelar contrato → transfere (não escreve tool como texto)
  Mensagem: "Quero cancelar meu contrato"
  Tool call: transferir_departamento(queue_id="453", user_id="815")
  Resposta: 
  Validações:
    ✅ Nenhuma tool chamada (permitidas: ['transferir_departamento'])
    ✅ Resposta NÃO contém 'transferir_departamento'
    ✅ Resposta NÃO contém 'queue_id'
    ✅ Resposta NÃO contém 'user_id'
    ✅ Resposta NÃO contém 'consultar_cliente'
    ✅ Resposta NÃO contém 'registrar_compromisso'
  Tempo: 1370ms
  Resultado: ✅ PASS

[TT4] Tool-as-text — já paguei → transfere financeiro (não escreve tool como texto)
  Mensagem: "Já paguei o boleto"
  Tool call: transferir_departamento(queue_id="454", user_id="814")
  Resposta: 
  Validações:
    ✅ Tool transferir_departamento chamada
    ✅ Resposta NÃO contém 'transferir_departamento('
    ✅ Resposta NÃO contém 'queue_id='
    ✅ Resposta NÃO contém 'user_id='
    ✅ Resposta NÃO contém 'consultar_cliente('
    ✅ Resposta NÃO contém 'registrar_compromisso('
  Tempo: 1297ms
  Resultado: ✅ PASS

[TT5] Tool-as-text — falar com humano → transfere (não escreve tool como texto)
  Mensagem: "Quero falar com uma pessoa de verdade"
  Tool call: transferir_departamento(queue_id="453", user_id="815")
  Resposta: Transfiro você para o atendimento, só um momento.
  Validações:
    ✅ Tool transferir_departamento chamada
    ✅ Resposta NÃO contém 'transferir_departamento('
    ✅ Resposta NÃO contém 'queue_id='
    ✅ Resposta NÃO contém 'user_id='
    ✅ Resposta NÃO contém 'consultar_cliente('
    ✅ Resposta NÃO contém 'registrar_compromisso('
  Tempo: 1571ms
  Resultado: ✅ PASS

[TT6] Tool-as-text — cidade fora da área → transfere (não escreve tool como texto)
  Mensagem: "Vocês atendem em Cuiabá?"
  Tool call: transferir_departamento(queue_id="453", user_id="815")
  Resposta: Transfiro você para o atendimento, só um momento.
  Validações:
    ✅ Nenhuma tool chamada (permitidas: ['transferir_departamento'])
    ✅ Resposta NÃO contém 'transferir_departamento'
    ✅ Resposta NÃO contém 'queue_id'
    ✅ Resposta NÃO contém 'user_id'
    ✅ Resposta NÃO contém 'não atendemos'
    ✅ Resposta NÃO contém 'não cobrimos'
  Tempo: 1399ms
  Resultado: ✅ PASS

[TT7] Tool-as-text — billing pagou → transfere (não escreve tool como texto)
  Mensagem: "Fiz o pix agora"
  Contexto: billing
  Tool call: transferir_departamento(queue_id="454", user_id="814")
  Resposta: Olá, Carlos! Passando para lembrar que sua mensalidade de R$ 189,90 vence em 03/04/2026.

Segue o link para pagamento:
https://sandbox.asaas.com/i/abc123

Qualquer dúvida, estou por aqui!
  Validações:
    ✅ Tool transferir_departamento chamada
    ✅ Resposta NÃO contém 'transferir_departamento('
    ✅ Resposta NÃO contém 'queue_id='
    ✅ Resposta NÃO contém 'user_id='
    ✅ Resposta NÃO contém 'consultar_cliente('
  Tempo: 1381ms
  Resultado: ✅ PASS

[TT8] Tool-as-text — defeito em contexto manutenção → transfere (não escreve tool como texto)
  Mensagem: "O ar parou de funcionar, não liga mais"
  Contexto: manutencao
  Tool call: transferir_departamento(queue_id="453", user_id="815")
  Resposta: Olá, Carlos! Está chegando a hora da manutenção preventiva do seu ar-condicionado!

*Equipamento:* Springer 12000 BTUs
*Endereço:* Rua das Flores, 123

A manutenção é gratuita e está inclusa no seu co...
  Validações:
    ✅ Tool transferir_departamento chamada
    ✅ Resposta NÃO contém 'transferir_departamento('
    ✅ Resposta NÃO contém 'queue_id='
    ✅ Resposta NÃO contém 'user_id='
  Tempo: 1157ms
  Resultado: ✅ PASS

============================================================
TOTAL: 8 | ✅ PASS: 8 | ❌ FAIL: 0
============================================================
```

### Execução 2: Suite completa

Comando: `PYTHONPATH=/var/www/ana-langgraph python3 .claude/skills/lead-simulator/scripts/simulate.py --all`
Resultado: `TOTAL: 76 | ✅ PASS: 63 | ❌ FAIL: 13`

Stdout da execução 2 não foi capturado por completo — só o `tail -80` (últimos 8 cenários). Os FAILs dos cenários anteriores não foram registrados individualmente.

---

## O que os outputs do simulate.py NÃO provam

1. **Não tenho o AIMessage bruto.** O simulate.py imprime `Tool call: transferir_departamento(queue_id="453", user_id="815")` — isso vem de `msg.tool_calls[0]["name"]` e `msg.tool_calls[0]["args"]`, não do `content`. Mas o objeto AIMessage completo (com `content` + `tool_calls` + `response_metadata`) não é salvo nem impresso.

2. **Não tenho as ToolMessages.** O simulate.py extrai tool_calls e resposta mas descarta as ToolMessage (role: tool). Não tenho prova de que a tool mock foi executada e retornou resultado — só que `tool_calls` estava presente no AIMessage.

3. **Não tenho como diferenciar function calling nativo de texto** a partir do output. Quando o simulate.py imprime `Tool call: transferir_departamento(...)`, isso confirma que `msg.tool_calls` tinha conteúdo. Mas quando imprime `Tool calls: nenhum` + `Resposta: [texto]`, isso confirma que `msg.tool_calls` estava vazio e o modelo respondeu com texto — mas não mostra se esse texto continha sintaxe de função (a validação `expect_not_contains` é quem verifica isso).

4. **Não tenho log de PM2** dos testes. O simulate.py roda como processo separado, não pelo PM2. O PM2 roda a API de produção.

---

## Lacunas e o que faria diferente

- Deveria ter rodado com `--report` para gerar JSON com `tool_calls` e `resposta` salvos em disco
- Deveria ter modificado o simulate.py para serializar o AIMessage bruto (incluindo `content`, `tool_calls`, `response_metadata.finish_reason`)
- Deveria ter criado teste unitário para `detectar_tool_como_texto()` com inputs sintéticos
- Deveria ter criado teste unitário para o interceptor em `grafo.py` com mock de AIMessage contendo tool-as-text

---

## Testes unitários da função `detectar_tool_como_texto`

Sem teste executado.

## Teste do interceptor bloqueante em `core/grafo.py`

Sem teste executado.

---

## Conclusão

| Ação | Cobertura de Teste | Evidência em disco |
|------|-------------------|--------------------|
| **Ação 3** (limpar prompt) | 8 cenários TT + 76 suite completa | **Nenhuma.** Output existe só no stdout desta conversa. Não foi rodado com `--report`. |
| **Ação 4** (interceptor) | **Sem teste** | Código implementado mas nunca acionado. Sem teste unitário. |

As duas ações estão sem evidência persistente em disco. O único registro é o stdout capturado nos tool results desta sessão do Claude Code.
