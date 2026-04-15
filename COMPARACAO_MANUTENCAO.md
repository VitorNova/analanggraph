# Comparação — Código de Disparo de Manutenção

**Produção:** `/var/www/ana-langgraph/`
**Referência:** `/root/organizacao/`
**Data:** 2026-04-15

## Resultado

**TODOS OS ARQUIVOS SÃO IDÊNTICOS (byte-a-byte).** Não há diferenças de código a reportar.

## Arquivos comparados (`diff -q`)

| Arquivo | Status |
|---|---|
| `jobs/manutencao_job.py` | IDÊNTICO |
| `infra/leadbox_client.py` | IDÊNTICO |
| `core/constants.py` | IDÊNTICO |
| `infra/supabase.py` | IDÊNTICO |
| `infra/nodes_supabase.py` | IDÊNTICO |
| `infra/redis.py` | IDÊNTICO |
| `infra/event_logger.py` | IDÊNTICO |
| `infra/incidentes.py` | IDÊNTICO |
| `core/context_detector.py` | IDÊNTICO |
| `ecosystem.config.js` | IDÊNTICO |

## Detalhes do fluxo (igual nos dois)

### Imports (`jobs/manutencao_job.py` L14-26)
```python
import asyncio
import sys
import logging
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from infra.supabase import get_supabase
from core.constants import TABLE_LEADS, TABLE_ASAAS_CLIENTES, TABLE_CONTRACT_DETAILS
```

### Template (L34-41)
```python
TEMPLATE = (
    "Olá, {nome}!\n\n"
    "Está chegando a hora da manutenção preventiva do seu ar-condicionado!\n\n"
    "*Equipamento:* {equipamento}\n"
    "*Endereço:* {endereco}\n\n"
    "A manutenção é gratuita e está inclusa no seu contrato.\n\n"
    "Quer agendar? Me fala um dia e horário de preferência!"
)
```

### Filtro Supabase (`buscar_contratos_d7`, L44-117)
- Query: `contract_details` onde `proxima_manutencao = hoje+7` AND `deleted_at IS NULL`
- Pula `maintenance_status == "notified"`
- Fallback de telefone: `locatario_telefone` → `asaas_clientes.mobile_phone` via `customer_id`
- Valida telefone ≥ 10 dígitos
- Formata equipamento: `"{marca} {btus} BTUs"` (+extras)

### Envio Leadbox (L238-246)
```python
from infra.leadbox_client import enviar_resposta_leadbox
from core.constants import QUEUE_MANUTENCAO, USER_IA
if not enviar_resposta_leadbox(tel_envio, message, raw=True,
                                queue_id=QUEUE_MANUTENCAO, user_id=USER_IA):
```
- Usa `raw=True` (texto livre, **não** template Meta)
- Fila: `QUEUE_MANUTENCAO=545`
- User: `USER_IA=1095`

### Anti-duplicata (L176)
```python
dedup_key = f"dispatch:{phone}:{context_type}:{contract_id}:{date.today().isoformat()}"
```
TTL 86400s

### Marcação pós-envio (L229-233)
```python
supabase.table(TABLE_CONTRACT_DETAILS).update({
    "maintenance_status": "notified",
    "notificacao_enviada_at": now,
}).eq("id", contract_id).execute()
```

## Conclusão

O código de disparo de manutenção em `/root/organizacao/` e o de produção em `/var/www/ana-langgraph/` estão **100% sincronizados**. Nenhuma linha difere entre as duas versões — nem no job em si, nem em nenhuma de suas dependências (leadbox_client, constants, supabase, nodes_supabase, redis, event_logger, incidentes, context_detector, ecosystem.config.js).
