"""Tools da Ana — Agente IA da Aluga-Ar.

2 tools ativas:
- consultar_cliente: Consulta dados, cobranças, contratos no Asaas
- transferir_departamento: Transfere para fila humana no Leadbox
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


def _get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return None
    return create_client(url, key)


@tool
def consultar_cliente(
    cpf: Optional[str] = None,
    verificar_pagamento: bool = False,
    buscar_por_telefone: bool = False,
    phone: Annotated[str, InjectedState("phone")] = "",
) -> str:
    """Consulta completa do cliente: dados pessoais, cobranças pendentes/atrasadas, contratos.

    Use quando o cliente perguntar sobre: pagamento, boleto, pix, fatura, segunda via,
    valor da parcela, parcelas atrasadas, quanto deve, contrato, equipamentos, manutenção.
    Se o cliente NAO veio por disparo de cobrança, pergunte o CPF primeiro.
    Se o cliente veio por disparo de cobrança/manutenção, use buscar_por_telefone=true (sem CPF).
    Se o cliente afirmar que já pagou, use verificar_pagamento=true.

    Args:
        cpf: CPF ou CNPJ (apenas números). Opcional se cliente veio por disparo.
        verificar_pagamento: Se true, busca faturas pagas recentemente.
        buscar_por_telefone: Se true, busca pelo telefone do lead. Use APENAS quando o cliente veio por disparo de cobrança ou manutenção.
    """
    supabase = _get_supabase()
    if not supabase:
        return "Erro: banco indisponível"

    customer_id = None
    customer_data = None

    # 1. Busca por CPF (se fornecido)
    if cpf:
        cpf_limpo = re.sub(r'\D', '', cpf)
        if len(cpf_limpo) not in [11, 14]:
            return "CPF inválido. Informe apenas os números (11 dígitos)."

        result = supabase.table("asaas_clientes").select(
            "id, name, cpf_cnpj, mobile_phone, email"
        ).eq("cpf_cnpj", cpf_limpo).is_("deleted_at", "null").limit(1).execute()

        if result.data:
            customer_data = result.data[0]
            customer_id = customer_data["id"]

    # 2. Busca por telefone (apenas quando explicitamente solicitado — leads de disparo)
    if not customer_id and not cpf and buscar_por_telefone and phone:
        phone_clean = re.sub(r'\D', '', phone)
        # Tenta variantes: com/sem 55, últimos 8-11 dígitos
        variantes = [phone_clean]
        if phone_clean.startswith("55") and len(phone_clean) > 11:
            variantes.append(phone_clean[2:])  # sem DDI
        if len(phone_clean) >= 8:
            variantes.append(phone_clean[-8:])  # últimos 8

        for variante in variantes:
            result = supabase.table("asaas_clientes").select(
                "id, name, cpf_cnpj, mobile_phone, email"
            ).ilike("mobile_phone", f"%{variante}%").is_("deleted_at", "null").limit(1).execute()

            if result.data:
                customer_data = result.data[0]
                customer_id = customer_data["id"]
                logger.info(f"[TOOL] Cliente encontrado por telefone: {variante}")
                break

    if not customer_id:
        if cpf:
            return "Não encontrei cadastro com esse CPF/CNPJ. Verifique se digitou corretamente."
        return "Para localizar seu cadastro, por favor informe seu CPF ou CNPJ."

    # Cobranças pendentes
    cobrancas = supabase.table("asaas_cobrancas").select(
        "id, value, due_date, status, invoice_url"
    ).eq("customer_id", customer_id).in_(
        "status", ["PENDING", "OVERDUE"]
    ).is_("deleted_at", "null").order("due_date").limit(10).execute()

    # Contratos ativos
    contratos = supabase.table("asaas_contratos").select(
        "descricao, valor_mensal, data_inicio, data_fim"
    ).eq("customer_id", customer_id).eq("status", "active").limit(5).execute()

    resp = f"Cliente: {customer_data.get('name', '?')}\nCPF: {customer_data.get('cpf_cnpj', '?')}\n\n"

    cobs = cobrancas.data or []
    if cobs:
        resp += f"{len(cobs)} cobrança(s) pendente(s):\n"
        for c in cobs:
            st = "VENCIDA" if c["status"] == "OVERDUE" else "PENDENTE"
            resp += f"- R$ {c['value']:.2f} | Vence: {c['due_date']} | {st}\n"
            if c.get("invoice_url"):
                resp += f"  Link: {c['invoice_url']}\n"
    else:
        resp += "Nenhuma cobrança pendente.\n"

    cts = contratos.data or []
    if cts:
        resp += f"\n{len(cts)} contrato(s) ativo(s):\n"
        for ct in cts:
            resp += f"- {ct.get('descricao', '?')} | R$ {ct.get('valor_mensal', 0):.2f}/mês\n"

    if verificar_pagamento:
        limite = (date.today() - timedelta(days=30)).isoformat()
        pagas = supabase.table("asaas_cobrancas").select(
            "value, due_date, payment_date"
        ).eq("customer_id", customer_id).in_(
            "status", ["RECEIVED", "CONFIRMED"]
        ).gte("payment_date", limite).order("payment_date", desc=True).limit(5).execute()

        if pagas.data:
            resp += f"\nPagamentos recentes:\n"
            for p in pagas.data:
                resp += f"- R$ {p['value']:.2f} | Pago: {p.get('payment_date', '?')}\n"
        else:
            resp += "\nNenhum pagamento nos últimos 30 dias.\n"

    return resp


@tool
def transferir_departamento(
    queue_id: int,
    user_id: int,
    phone: Annotated[str, InjectedState("phone")] = "",
) -> str:
    """Transfere o atendimento para outro departamento no Leadbox CRM.

    NUNCA avise o cliente antes de transferir. Apenas transfira silenciosamente.
    O telefone é injetado automaticamente do contexto da conversa.

    Args:
        queue_id: ID da fila de destino. Use: 453 (Atendimento), 454 (Financeiro), 544 (Cobranças). NUNCA use 537 (fila da IA).
        user_id: ID do atendente. Use: 815 (Nathália), 813 (Lázaro), 814 (Tieli).

    Returns:
        Confirmação de transferência ou erro
    """
    LEADBOX_URL = "https://enterprise-135api.leadbox.app.br"
    LEADBOX_UUID = os.environ.get("LEADBOX_API_UUID", "")
    LEADBOX_TOKEN = os.environ.get("LEADBOX_API_TOKEN", "")

    if not LEADBOX_UUID or not LEADBOX_TOKEN:
        return "Erro: credenciais Leadbox não configuradas (LEADBOX_API_UUID/LEADBOX_API_TOKEN)"

    if queue_id == 537:
        return "Não pode transferir para fila da IA"

    telefone_limpo = re.sub(r"[^\d]", "", phone)

    try:
        push_url = f"{LEADBOX_URL}/v1/api/external/{LEADBOX_UUID}/?token={LEADBOX_TOKEN}"
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                push_url,
                headers={
                    "Content-Type": "application/json",
                },
                json={
                    "number": telefone_limpo,
                    "body": "O departamento ideal vai dar continuidade ao seu atendimento.",
                    "queueId": queue_id,
                    "userId": user_id,
                    "forceTicketToDepartment": True,
                    "forceTicketToUser": True,
                },
            )
            resp.raise_for_status()
        return f"Transferido para fila {queue_id} com sucesso"
    except Exception as e:
        return f"Erro ao transferir: {e}"


TOOLS = [consultar_cliente, transferir_departamento]
