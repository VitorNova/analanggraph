"""Constantes centralizadas da integração Leadbox e projeto Ana."""

import os

# ── Tabela Supabase ──
TABLE_LEADS = "ana_leads"

# ── Leadbox API ──
LEADBOX_API_URL = os.environ.get(
    "LEADBOX_API_URL", "https://enterprise-135api.leadbox.app.br"
)
LEADBOX_API_UUID = os.environ.get("LEADBOX_API_UUID", "")
LEADBOX_API_TOKEN = os.environ.get("LEADBOX_API_TOKEN", "")

# ── Tabelas Asaas (compartilhadas com lazaro-real) ──
TABLE_ASAAS_CLIENTES = "asaas_clientes"
TABLE_ASAAS_COBRANCAS = "asaas_cobrancas"
TABLE_ASAAS_CONTRATOS = "asaas_contratos"
TABLE_CONTRACT_DETAILS = "contract_details"

# ── Leadbox IDs (tenant Aluga-Ar) ──
TENANT_ID = 123
QUEUE_IA = 537
QUEUE_BILLING = 544
QUEUE_MANUTENCAO = 545
IA_QUEUES = {QUEUE_IA, QUEUE_BILLING, QUEUE_MANUTENCAO}
USER_IA = 1095  # Usuário "Ana" no Leadbox
