"""Tools da Ana — Agente IA da Aluga-Ar.

Otimizadas para Gemini 2.0 Flash seguindo best practices OFICIAIS do Google:
- FunctionDeclaration manual para controle total do schema
- Enum para valores finitos (queue_id, user_id)
- Descrições detalhadas com QUANDO USAR e exemplos
- Validações robustas com mensagens informativas

3 tools ativas:
- consultar_cliente: Consulta dados, cobranças, contratos no Asaas
- transferir_departamento: Transfere para fila humana no Leadbox
- registrar_compromisso: Registra promessa de pagamento e silencia cobranças

Referência: https://ai.google.dev/gemini-api/docs/function-calling
"""

import logging
import os
import re
from datetime import date, timedelta
from typing import Optional
from typing_extensions import Annotated

import httpx
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from supabase import create_client

logger = logging.getLogger(__name__)

# Tabela de leads no Supabase
TABLE_LEADS = "ana_leads"


def _get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return None
    return create_client(url, key)


# =============================================================================
# TOOL 1: CONSULTAR CLIENTE
# =============================================================================

@tool
def consultar_cliente(
    cpf: Optional[str] = None,
    verificar_pagamento: bool = False,
    buscar_por_telefone: bool = False,
    phone: Annotated[str, InjectedState("phone")] = "",
) -> str:
    """Consulta dados do cliente no sistema: informações pessoais, cobranças pendentes/atrasadas e contratos ativos de aluguel de ar-condicionado.

QUANDO USAR ESTA TOOL:

→ Cliente pergunta sobre pagamento, boleto, pix, fatura, segunda via:
  - Pergunte o CPF primeiro (se ainda não tiver)
  - Chame com cpf="12345678900"

→ Cliente pergunta quanto deve, parcelas atrasadas, valor da mensalidade:
  - Pergunte o CPF primeiro (se ainda não tiver)
  - Chame com cpf="12345678900"

→ Cliente pergunta sobre seu contrato ou equipamentos instalados:
  - Pergunte o CPF primeiro (se ainda não tiver)
  - Chame com cpf="12345678900"

→ Cliente diz que já pagou ("já paguei", "fiz o pagamento", "já transferi"):
  - NÃO use esta tool. Transfira para financeiro com transferir_departamento(destino="financeiro").

→ Cliente veio por DISPARO de cobrança ou manutenção (mensagem automática do sistema):
  - NÃO peça CPF, use o telefone automaticamente
  - Chame com buscar_por_telefone=true

→ Cliente quer saber sobre manutenção preventiva do ar dele:
  - Pergunte o CPF primeiro
  - Chame com cpf="12345678900" para identificar o contrato e equipamento

COMO IDENTIFICAR QUE CLIENTE VEIO POR DISPARO DE COBRANÇA:
- A primeira mensagem do cliente é uma resposta direta a uma cobrança automática
- Cliente responde algo como "oi", "ok", "vou pagar", "pode mandar o pix" sem contexto prévio
- O histórico mostra que o sistema enviou mensagem de cobrança antes do cliente responder

REGRAS:
- CPF deve ter 11 dígitos (pessoa física) ou 14 dígitos (CNPJ empresa)
- Se cliente informar CPF com pontos/traços, aceite normalmente — a limpeza é automática
- Se não encontrar o cliente, informe que não localizou e peça para verificar o CPF
- Após consultar, use as informações para responder ao cliente de forma clara

EXEMPLOS DE USO:

Exemplo 1: Cliente diz "quero o boleto" e você ainda não tem o CPF
→ Primeiro pergunte: "Me passa seu CPF, por favor?"
→ Cliente responde: "123.456.789-00"
→ Chamar: cpf="12345678900"

Exemplo 2: Cliente diz "já paguei a fatura"
→ NÃO use consultar_cliente. Use transferir_departamento(destino="financeiro").

Exemplo 3: Cliente veio por disparo de cobrança e disse "vou pagar amanhã"
→ Chamar: buscar_por_telefone=true
→ NÃO peça CPF — o sistema já sabe quem é pelo telefone

Exemplo 4: Cliente pergunta "quantos ares eu tenho alugado?"
→ Primeiro pergunte: "Me passa seu CPF pra eu localizar seu contrato?"
→ Cliente responde: "11122233344"
→ Chamar: cpf="11122233344"

    Args:
        cpf: CPF (11 dígitos) ou CNPJ (14 dígitos) do cliente. Apenas números, pontos e traços são removidos automaticamente. Obrigatório exceto quando buscar_por_telefone=true.
        verificar_pagamento: Se true, busca pagamentos recebidos nos últimos 30 dias. NÃO usar quando cliente diz "já paguei" — nesse caso, transferir pro financeiro.
        buscar_por_telefone: Se true, busca o cliente pelo telefone da conversa ao invés do CPF. Use APENAS quando cliente veio por disparo automático de cobrança ou manutenção.

    Returns:
        Dados do cliente formatados: nome, CPF, cobranças pendentes com links de boleto/pix, contratos ativos com quantidade de ares. Ou mensagem de erro se não encontrar.
    """
    supabase = _get_supabase()
    if not supabase:
        logger.error("[TOOL] Supabase indisponível")
        return "Erro: sistema temporariamente indisponível. Tente novamente em alguns minutos."

    customer_id = None
    customer_data = None

    # 1. Busca por CPF (se fornecido)
    if cpf:
        cpf_limpo = re.sub(r'\D', '', cpf)

        if len(cpf_limpo) == 0:
            return "CPF inválido. Informe apenas os números (11 dígitos para CPF ou 14 para CNPJ)."

        if len(cpf_limpo) not in [11, 14]:
            return f"CPF/CNPJ inválido. Você informou {len(cpf_limpo)} dígitos, mas deve ter 11 (CPF) ou 14 (CNPJ)."

        result = supabase.table("asaas_clientes").select(
            "id, name, cpf_cnpj, mobile_phone, email"
        ).eq("cpf_cnpj", cpf_limpo).is_("deleted_at", "null").limit(1).execute()

        if result.data:
            customer_data = result.data[0]
            customer_id = customer_data["id"]
            logger.info(f"[TOOL] Cliente encontrado por CPF: {cpf_limpo[:3]}***")

    # 2. Busca por telefone (apenas quando explicitamente solicitado — leads de disparo)
    if not customer_id and not cpf and buscar_por_telefone and phone:
        phone_clean = re.sub(r'\D', '', phone)

        # Tenta variantes: com/sem 55, últimos 8-11 dígitos
        variantes = [phone_clean]
        if phone_clean.startswith("55") and len(phone_clean) > 11:
            variantes.append(phone_clean[2:])  # sem DDI
        if len(phone_clean) >= 8:
            variantes.append(phone_clean[-8:])  # últimos 8
            variantes.append(phone_clean[-9:])  # últimos 9

        for variante in variantes:
            result = supabase.table("asaas_clientes").select(
                "id, name, cpf_cnpj, mobile_phone, email"
            ).ilike("mobile_phone", f"%{variante}%").is_("deleted_at", "null").limit(1).execute()

            if result.data:
                customer_data = result.data[0]
                customer_id = customer_data["id"]
                logger.info(f"[TOOL] Cliente encontrado por telefone: ***{variante[-4:]}")
                break

    # 3. Não encontrou
    if not customer_id:
        if cpf:
            cpf_limpo = re.sub(r'\D', '', cpf)
            return f"Não encontrei cadastro com o CPF/CNPJ {cpf_limpo[:3]}***{cpf_limpo[-2:]}. Pode verificar se digitou corretamente?"
        if buscar_por_telefone:
            return "Não encontrei seu cadastro pelo telefone. Pode me informar seu CPF ou CNPJ?"
        return "Para localizar seu cadastro, preciso do seu CPF ou CNPJ."

    # 4. Busca cobranças pendentes
    cobrancas = supabase.table("asaas_cobrancas").select(
        "id, value, due_date, status, invoice_url"
    ).eq("customer_id", customer_id).in_(
        "status", ["PENDING", "OVERDUE"]
    ).is_("deleted_at", "null").order("due_date").limit(10).execute()

    # 5. Busca contratos ativos
    contratos = supabase.table("asaas_contratos").select(
        "description, value, next_due_date, qtd_ars"
    ).eq("customer_id", customer_id).eq("status", "ACTIVE").limit(5).execute()

    # 6. Monta resposta estruturada
    resp = f"DADOS DO CLIENTE:\n"
    resp += f"Nome: {customer_data.get('name', 'Não informado')}\n"
    resp += f"CPF/CNPJ: {customer_data.get('cpf_cnpj', 'Não informado')}\n\n"

    # Cobranças
    cobs = cobrancas.data or []
    if cobs:
        resp += f"COBRANÇAS PENDENTES ({len(cobs)}):\n"
        for c in cobs:
            status_texto = "⚠️ VENCIDA" if c["status"] == "OVERDUE" else "📅 Pendente"
            resp += f"- R$ {c['value']:.2f} | Vencimento: {c['due_date']} | {status_texto}\n"
            if c.get("invoice_url"):
                resp += f"  Link do boleto/pix: {c['invoice_url']}\n"
    else:
        resp += "COBRANÇAS PENDENTES: Nenhuma ✅\n"

    # Contratos
    cts = contratos.data or []
    if cts:
        resp += f"\nCONTRATOS ATIVOS ({len(cts)}):\n"
        for ct in cts:
            qtd = ct.get('qtd_ars', '?')
            resp += f"- {ct.get('description', 'Contrato')} | R$ {ct.get('value', 0):.2f}/mês | {qtd} ar(es) instalado(s)\n"
    else:
        resp += "\nCONTRATOS ATIVOS: Nenhum\n"

    # 7. Busca pagamentos recentes (se solicitado)
    if verificar_pagamento:
        limite = (date.today() - timedelta(days=30)).isoformat()
        pagas = supabase.table("asaas_cobrancas").select(
            "value, due_date, payment_date"
        ).eq("customer_id", customer_id).in_(
            "status", ["RECEIVED", "CONFIRMED"]
        ).gte("payment_date", limite).order("payment_date", desc=True).limit(5).execute()

        if pagas.data:
            resp += f"\nPAGAMENTOS RECENTES (últimos 30 dias):\n"
            for p in pagas.data:
                resp += f"- R$ {p['value']:.2f} | Pago em: {p.get('payment_date', '?')} ✅\n"
        else:
            resp += "\nPAGAMENTOS RECENTES: Nenhum nos últimos 30 dias ❌\n"

    return resp


# =============================================================================
# TOOL 2: TRANSFERIR DEPARTAMENTO
# =============================================================================

# Mapeamento de destinos para validação e logs
DESTINOS_TRANSFERENCIA = {
    (453, 815): "Nathália (Atendimento)",
    (453, 813): "Lázaro (Dono)",
    (454, 814): "Tieli (Financeiro)",
    (544, 814): "Tieli (Cobranças)",
}

# Mapeamento destino → (queue_id, user_id, nome) — usado pela tool e pelo interceptor
MAPA_DESTINOS = {
    "atendimento": (453, 815, "Nathália (Atendimento)"),
    "financeiro": (454, 814, "Tieli (Financeiro)"),
    "cobrancas": (544, 814, "Tieli (Cobranças)"),
    "lazaro": (453, 813, "Lázaro (Dono)"),
}


@tool
def transferir_departamento(
    destino: str,
    phone: Annotated[str, InjectedState("phone")] = "",
) -> str:
    """Transfere o atendimento para outro departamento no Leadbox CRM.

REGRAS ABSOLUTAS:
- NUNCA envie mensagem ao cliente antes de transferir. Chame esta tool silenciosamente.
- EXECUTE a tool imediatamente. NUNCA diga "vou transferir" sem chamar a ferramenta. NUNCA peça confirmação ("ok?", "pode ser?") antes de transferir.
- O telefone é injetado automaticamente do contexto.

QUANDO USAR ESTA TOOL:

→ destino="atendimento" (Nathália):
  - Novo aluguel: após coletar nome completo e CPF do cliente
  - Retirada de equipamento: cliente quer devolver ar, mudar de casa, cancelar → transferir IMEDIATAMENTE sem pedir CPF
  - Manutenção/Defeito: ar quebrou, parou de funcionar, não gela, pingando, barulho estranho, cheiro ruim
  - Assistência técnica
  - Reclamação ou insatisfação do cliente
  - Cancelamento de contrato
  - Cliente pede para falar com humano ou atendente
  - Cliente fora da área de cobertura (menciona cidade que NÃO é Rondonópolis ou Primavera do Leste) → transferir SEM dizer "não atendemos"
  - Pergunta que você não sabe responder
  - Cliente atual precisando de suporte técnico

→ destino="financeiro" (Tieli):
  - Cliente diz que já pagou ("já paguei", "paguei ontem", "fiz o pix", "já transferi")
  - Cliente envia comprovante de pagamento
  - Cliente informou que tem restrição no CPF ou CNPJ (nome sujo, negativado)
  - Cliente já é cliente E consultar_cliente não resolveu a dúvida financeira
  - Negociação de dívida ou parcelamento especial

→ destino="cobrancas" (Tieli):
  - Dúvidas sobre valores cobrados
  - Contestação de fatura (valor errado, cobrança indevida)
  - Cliente diz que não reconhece cobrança
  - Fatura não encontrada após usar consultar_cliente

→ destino="lazaro" (Lázaro/Dono):
  - Cliente pede para falar com o dono, proprietário ou Lázaro
  - Assuntos que só o dono pode resolver
  - Reclamações graves

EXEMPLOS DE USO:

Exemplo 1: Cliente diz "quero alugar" e já informou nome "João Silva" e CPF "12345678900"
→ Chamar: destino="atendimento"

Exemplo 2: Cliente diz "quero devolver o ar" ou "vou mudar de endereço" ou "quero cancelar"
→ Chamar IMEDIATAMENTE (sem pedir CPF): destino="atendimento"

Exemplo 3: Cliente diz "meu ar quebrou" ou "o ar tá pingando" ou "não tá gelando" ou "tá fazendo barulho"
→ Chamar IMEDIATAMENTE: destino="atendimento"

Exemplo 4: Cliente diz "tenho restrição no nome" ou "meu CPF tá sujo" ou "estou negativado"
→ Chamar: destino="financeiro"

Exemplo 5: Cliente enviou foto de comprovante mas consultar_cliente ainda mostra fatura pendente
→ Chamar: destino="financeiro"

Exemplo 6: Cliente diz "não concordo com essa cobrança" ou "esse valor tá errado" ou "não reconheço essa fatura"
→ Chamar: destino="cobrancas"

Exemplo 7: Cliente diz "quero falar com o Lázaro" ou "quero falar com o dono" ou "passa pro proprietário"
→ Chamar: destino="lazaro"

Exemplo 8: Cliente pergunta algo que você não sabe responder
→ Chamar: destino="atendimento"

    Args:
        destino: Departamento de destino. Valores válidos: "atendimento", "financeiro", "cobrancas", "lazaro".

    Returns:
        Confirmação de transferência ou mensagem de erro.
    """
    LEADBOX_URL = "https://enterprise-135api.leadbox.app.br"
    LEADBOX_UUID = os.environ.get("LEADBOX_API_UUID", "")
    LEADBOX_TOKEN = os.environ.get("LEADBOX_API_TOKEN", "")

    if not LEADBOX_UUID or not LEADBOX_TOKEN:
        logger.error("[TOOL] Credenciais Leadbox não configuradas")
        return "Erro: credenciais Leadbox não configuradas"

    # Validação do destino
    destino_lower = destino.lower().strip()
    if destino_lower not in MAPA_DESTINOS:
        logger.warning(f"[TOOL] Destino inválido: {destino}")
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "transferencia_falhou", f"Destino inválido: {destino}", {"destino": destino})
        return f"Erro: destino '{destino}' inválido. Use: atendimento, financeiro, cobrancas ou lazaro."

    queue_id, user_id, destino_nome = MAPA_DESTINOS[destino_lower]

    # Validação: telefone presente
    telefone_limpo = re.sub(r"[^\d]", "", phone)
    if not telefone_limpo:
        logger.error("[TOOL] Telefone não disponível no contexto")
        return "Erro: telefone não disponível"

    try:
        push_url = f"{LEADBOX_URL}/v1/api/external/{LEADBOX_UUID}/?token={LEADBOX_TOKEN}"

        with httpx.Client(timeout=15) as client:
            resp = client.post(
                push_url,
                headers={"Content-Type": "application/json"},
                json={
                    "number": telefone_limpo,
                    "externalKey": telefone_limpo,
                    # SEM "body" = transferência 100% silenciosa
                    "queueId": queue_id,
                    "userId": user_id,
                    "forceTicketToDepartment": True,
                    "forceTicketToUser": True,
                },
            )
            resp.raise_for_status()

        # Marker anti-eco: sinaliza que este fromMe é da IA
        from infra.leadbox_client import _mark_sent_by_ia
        _mark_sent_by_ia(telefone_limpo)

        logger.info(f"[TOOL] Transferência OK: {phone} → {destino_nome}")
        return f"Transferido para {destino_nome} com sucesso"

    except httpx.HTTPStatusError as e:
        logger.error(f"[TOOL] HTTP {e.response.status_code} ao transferir {phone} → {destino_nome}: {e}")
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "transferencia_falhou", f"HTTP {e.response.status_code}", {"destino": destino})
        return f"Erro HTTP {e.response.status_code} ao transferir para {destino_nome}"

    except httpx.TimeoutException:
        logger.error(f"[TOOL] Timeout ao transferir {phone} → {destino_nome}")
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "transferencia_falhou", "Timeout", {"destino": destino})
        return f"Erro: timeout ao transferir para {destino_nome}. Tente novamente."

    except Exception as e:
        logger.error(f"[TOOL] Erro ao transferir {phone} → {destino_nome}: {e}", exc_info=True)
        from infra.incidentes import registrar_incidente
        registrar_incidente(phone, "transferencia_falhou", str(e)[:300], {"destino": destino})
        return f"Erro ao transferir para {destino_nome}: {str(e)[:100]}"


# =============================================================================
# TOOL 3: REGISTRAR COMPROMISSO
# =============================================================================

@tool
def registrar_compromisso(
    data_prometida: str,
    phone: Annotated[str, InjectedState("phone")] = "",
) -> str:
    """Registra que o cliente prometeu pagar em uma data específica. Silencia cobranças automáticas até essa data.

QUANDO USAR ESTA TOOL:

→ Cliente promete pagar em um dia específico:
  - "vou pagar sexta"
  - "pago amanhã"
  - "resolvo essa semana"
  - "dia 15 eu pago"
  - "semana que vem eu acerto"

→ Cliente pede um prazo para pagar:
  - "me dá até sexta?"
  - "posso pagar só no dia 20?"
  - "consigo pagar depois do dia 10?"

COMO CONVERTER A FALA DO CLIENTE PARA DATA:

- "amanhã" → data de amanhã (ex: se hoje é 11/04, use "2026-04-12")
- "sexta" / "sexta-feira" → próxima sexta-feira
- "segunda" → próxima segunda-feira
- "semana que vem" → próxima segunda-feira
- "essa semana" → próxima sexta-feira
- "dia 15" → dia 15 do mês atual (ou próximo mês se já passou)
- "fim do mês" → último dia útil do mês atual
- "depois do dia 5" → dia 6 do próximo mês

REGRAS:
- A data deve estar no formato YYYY-MM-DD (ex: "2026-04-15")
- A data não pode ser no passado
- A data não pode ser mais de 30 dias no futuro
- Se cliente não especificar data clara, NÃO use esta tool — apenas confirme que ele vai pagar

EXEMPLOS DE USO:

Exemplo 1: Hoje é quarta 09/04 e cliente diz "vou pagar sexta"
→ Chamar: data_prometida="2026-04-11"

Exemplo 2: Hoje é 11/04 e cliente diz "pago amanhã"
→ Chamar: data_prometida="2026-04-12"

Exemplo 3: Hoje é 11/04 e cliente diz "dia 20 eu pago"
→ Chamar: data_prometida="2026-04-20"

Exemplo 4: Hoje é 25/04 e cliente diz "dia 5"
→ Chamar: data_prometida="2026-05-05" (próximo mês pois dia 5 de abril já passou)

Exemplo 5: Cliente diz "vou tentar pagar" (sem data específica)
→ NÃO chame esta tool — apenas responda "Combinado, fico no aguardo!"

    Args:
        data_prometida: Data em formato ISO YYYY-MM-DD. Converta a fala do cliente para a data real. Máximo 30 dias no futuro.

    Returns:
        Confirmação do compromisso registrado com a quantidade de dias até a data.
    """
    # Validação do formato da data
    try:
        target = date.fromisoformat(data_prometida)
    except (ValueError, TypeError):
        return f"Data inválida: '{data_prometida}'. Use formato YYYY-MM-DD (ex: 2026-04-15)."

    hoje = date.today()

    # Validação: data não pode ser no passado
    if target < hoje:
        return f"Data {data_prometida} já passou. Compromisso não registrado. Pergunte ao cliente uma nova data."

    # Validação: máximo 30 dias no futuro
    dias = (target - hoje).days
    if dias > 30:
        return f"Data muito distante ({data_prometida} = {dias} dias). Máximo permitido: 30 dias. Pergunte uma data mais próxima."

    # Salva no Supabase
    supabase = _get_supabase()
    if supabase and phone:
        try:
            phone_clean = re.sub(r'\D', '', phone)
            supabase.table(TABLE_LEADS).update({
                "billing_snooze_until": data_prometida,
            }).eq("telefone", phone_clean).execute()
            logger.info(f"[TOOL] Compromisso registrado: {phone} → {data_prometida} ({dias} dias)")
        except Exception as e:
            logger.warning(f"[TOOL] Erro ao salvar snooze no Supabase: {e}")
            from infra.incidentes import registrar_incidente
            registrar_incidente(phone, "snooze_falhou", f"Supabase update falhou: {e}"[:300], {"data": data_prometida})

    return f"Compromisso registrado: cobranças automáticas silenciadas até {data_prometida} ({dias} dia{'s' if dias != 1 else ''})."


# =============================================================================
# EXPORTAÇÃO
# =============================================================================

TOOLS = [consultar_cliente, transferir_departamento, registrar_compromisso]


# =============================================================================
# FUNCTION DECLARATIONS (para uso direto com Gemini API se necessário)
# =============================================================================
# Se você precisar usar diretamente com a API do Gemini (sem LangChain),
# aqui estão as FunctionDeclarations com enum correto:

GEMINI_FUNCTION_DECLARATIONS = [
    {
        "name": "consultar_cliente",
        "description": """Consulta dados do cliente no sistema: informações pessoais, cobranças pendentes/atrasadas e contratos ativos de aluguel de ar-condicionado.

QUANDO USAR:
- Cliente pergunta sobre pagamento, boleto, pix, fatura, segunda via
- Cliente pergunta quanto deve, parcelas atrasadas, valor da mensalidade
- Cliente pergunta sobre seu contrato ou equipamentos instalados
- Cliente diz que já pagou → NÃO usar esta tool, transferir pro financeiro
- Cliente veio por disparo de cobrança (use buscar_por_telefone=true)

REGRAS:
- Pergunte o CPF primeiro se não tiver
- Se cliente veio por disparo, use buscar_por_telefone=true sem pedir CPF""",
        "parameters": {
            "type": "object",
            "properties": {
                "cpf": {
                    "type": "string",
                    "description": "CPF (11 dígitos) ou CNPJ (14 dígitos) do cliente. Apenas números."
                },
                "verificar_pagamento": {
                    "type": "boolean",
                    "description": "Se true, busca pagamentos recebidos nos últimos 30 dias. Use quando cliente afirmar que já pagou."
                },
                "buscar_por_telefone": {
                    "type": "boolean",
                    "description": "Se true, busca pelo telefone da conversa. Use APENAS quando cliente veio por disparo de cobrança."
                }
            }
        }
    },
    {
        "name": "transferir_departamento",
        "description": """Transfere o atendimento para outro departamento no Leadbox CRM. NUNCA avise o cliente antes de transferir.

QUANDO USAR:
- atendimento: novo aluguel (após nome+CPF), retirada, manutenção, defeito, ar quebrado, reclamação, cliente pede humano
- financeiro: restrição no CPF, comprovante mas fatura pendente, negociação de dívida
- cobrancas: contestação de fatura, valor errado, não reconhece cobrança
- lazaro: cliente pede falar com dono/Lázaro""",
        "parameters": {
            "type": "object",
            "properties": {
                "destino": {
                    "type": "string",
                    "enum": ["atendimento", "financeiro", "cobrancas", "lazaro"],
                    "description": "Departamento de destino da transferência."
                }
            },
            "required": ["destino"]
        }
    },
    {
        "name": "registrar_compromisso",
        "description": """Registra que o cliente prometeu pagar em uma data específica. Silencia cobranças automáticas até essa data.

QUANDO USAR:
- Cliente promete pagar em dia específico: "vou pagar sexta", "pago amanhã", "dia 15 eu pago"
- Cliente pede prazo: "me dá até sexta?", "posso pagar dia 20?"

COMO CONVERTER:
- "amanhã" → data de amanhã
- "sexta" → próxima sexta-feira
- "dia 15" → dia 15 do mês atual (ou próximo se já passou)

NÃO use se cliente não especificar data clara.""",
        "parameters": {
            "type": "object",
            "properties": {
                "data_prometida": {
                    "type": "string",
                    "description": "Data em formato YYYY-MM-DD. Máximo 30 dias no futuro."
                }
            },
            "required": ["data_prometida"]
        }
    }
]
