#!/usr/bin/env python3
"""Lead Simulator — testa a Ana (Aluga-Ar) end-to-end com Gemini real e mocks de infra.

Uso:
    PYTHONPATH=. .venv/bin/python3 .claude/skills/lead-simulator/scripts/simulate.py B1
    PYTHONPATH=. .venv/bin/python3 .claude/skills/lead-simulator/scripts/simulate.py --group billing
    PYTHONPATH=. .venv/bin/python3 .claude/skills/lead-simulator/scripts/simulate.py --all
    PYTHONPATH=. .venv/bin/python3 .claude/skills/lead-simulator/scripts/simulate.py --adhoc "Manda o pix" --context billing --expect-not-contains "CPF"
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

# Adicionar raiz do projeto ao path
PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("lead-simulator")

MT_TZ = timezone(timedelta(hours=-4))


# =============================================================================
# DADOS MOCKADOS
# =============================================================================

LEAD = {"nome": "Carlos Souza", "telefone": "5566999881234"}

MOCK_CUSTOMER = {
    "id": "cus_mock123",
    "name": "Carlos Souza",
    "cpf_cnpj": "12345678901",
    "mobile_phone": "66999881234",
    "email": "carlos@email.com",
}

MOCK_COBRANCAS_PENDENTES = [{
    "id": "pay_abc123",
    "value": 189.90,
    "due_date": "2026-04-03",
    "status": "PENDING",
    "invoice_url": "https://sandbox.asaas.com/i/abc123",
}]

MOCK_COBRANCAS_PAGAS = [{
    "value": 189.90,
    "due_date": "2026-03-03",
    "payment_date": "2026-03-02",
}]

MOCK_CONTRATOS = [{
    "descricao": "Aluguel Split 12000 BTUs - Rua das Flores 123",
    "valor_mensal": 189.90,
    "data_inicio": "2025-10-01",
    "data_fim": "2026-10-01",
}]

BILLING_LINK = "https://sandbox.asaas.com/i/abc123"

NOW_ISO = datetime.now(timezone.utc).isoformat()
TWO_HOURS_AGO = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()


def _billing_history() -> dict:
    """Histórico com disparo de cobrança (billing)."""
    return {
        "messages": [{
            "role": "model",
            "content": (
                f"Olá, Carlos! Passando para lembrar que sua mensalidade de "
                f"R$ 189,90 vence em 03/04/2026.\n\n"
                f"Segue o link para pagamento:\n{BILLING_LINK}\n\n"
                f"Qualquer dúvida, estou por aqui!"
            ),
            "timestamp": TWO_HOURS_AGO,
            "context": "billing",
            "reference_id": "pay_abc123",
        }]
    }


def _manutencao_history() -> dict:
    """Histórico com disparo de manutenção preventiva."""
    return {
        "messages": [{
            "role": "model",
            "content": (
                "Olá, Carlos! Está chegando a hora da manutenção preventiva "
                "do seu ar-condicionado!\n\n"
                "*Equipamento:* Springer 12000 BTUs\n"
                "*Endereço:* Rua das Flores, 123\n\n"
                "A manutenção é gratuita e está inclusa no seu contrato.\n\n"
                "Quer agendar? Me fala um dia e horário de preferência!"
            ),
            "timestamp": TWO_HOURS_AGO,
            "context": "manutencao_preventiva",
            "contract_id": "contract_xyz",
        }]
    }


# =============================================================================
# CENÁRIOS
# =============================================================================

SCENARIOS = {
    # ── Billing — cobrança / Pix ──
    "B1": {
        "nome": "Billing — lead responde disparo com dúvida",
        "grupo": "billing",
        "mensagem": "Quanto tá minha fatura?",
        "context": "billing",
        "expect_tools": ["consultar_cliente"],
        "expect_not_contains": [
            "CPF", "cpf",
            "Aqui é a Ana", "sou a Ana", "Aluga-Ar!",
            "Como posso te ajudar hoje", "tudo bem?",
        ],
    },
    "B2": {
        "nome": "Billing — lead pede Pix/link (link já no histórico)",
        "grupo": "billing",
        "mensagem": "Manda o pix",
        "context": "billing",
        "expect_tools": [],
        "allow_tools": ["consultar_cliente"],
        "expect_contains": ["sandbox.asaas.com/i/abc123"],
        "expect_not_contains": ["LINK DO HISTORICO", "{link}", "link_placeholder"],
    },
    "B3": {
        "nome": "Billing — lead pede boleto SEM contexto (orgânico)",
        "grupo": "billing",
        "mensagem": "Quero meu boleto",
        "context": None,
        "expect_tools": [],
        "expect_contains_any": ["CPF", "cpf", "identificar", "localizar", "cadastro"],
        "expect_not_contains": [],
    },
    "B4": {
        "nome": "Billing — lead afirma que pagou (com contexto) → financeiro",
        "grupo": "billing",
        "mensagem": "Já paguei ontem",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "allow_tools": ["consultar_cliente"],
        "expect_args": {"transferir_departamento": {"destino": "financeiro"}},
        "expect_not_contains": ["CPF", "cpf"],
    },
    "B5": {
        "nome": "Billing — lead afirma que pagou SEM contexto → financeiro",
        "grupo": "billing",
        "mensagem": "Já paguei meu boleto ontem",
        "context": None,
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "financeiro"}},
        "expect_not_contains": ["erro", "error"],
    },
    "B6": {
        "nome": "Billing — lead quer negociar → financeiro",
        "grupo": "billing",
        "mensagem": "Tô sem condição de pagar, preciso negociar",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "financeiro"}},
    },
    "B7": {
        "nome": "Billing — lead diz 'vou pagar depois' (compromisso vago)",
        "grupo": "billing",
        "mensagem": "vou pagar depois, essa semana eu resolvo",
        "context": "billing",
        "expect_tools": [],
        "allow_tools": ["consultar_cliente", "registrar_compromisso"],
        "expect_not_contains": ["CPF", "cpf", "erro"],
        "expect_contains_any": ["sandbox.asaas.com", "link", "pix", "boleto", "pagamento", "certo", "combinado", "tudo bem", "ok"],
    },
    "B8": {
        "nome": "Billing — lead pede boleto do mês passado (com contexto)",
        "grupo": "billing",
        "mensagem": "quero o boleto do mês passado, não o desse",
        "context": "billing",
        "expect_tools": ["consultar_cliente"],
        "expect_not_contains": ["CPF", "cpf"],
    },
    "B9": {
        "nome": "Billing — lead pede Pix SEM contexto de disparo → pede CPF",
        "grupo": "billing",
        "mensagem": "manda o pix pra mim",
        "context": None,
        "expect_tools": [],
        "allow_tools": ["consultar_cliente"],
        "expect_contains_any": ["CPF", "cpf", "identificar", "localizar", "cadastro"],
        "expect_not_contains": ["erro"],
    },
    "B10": {
        "nome": "Billing — lead quer saber quanto deve (com contexto)",
        "grupo": "billing",
        "mensagem": "quanto que eu devo no total?",
        "context": "billing",
        "expect_tools": ["consultar_cliente"],
        "expect_not_contains": ["CPF", "cpf"],
    },
    "B11": {
        "nome": "Billing — lead recusa pagar → Lázaro",
        "grupo": "billing",
        "mensagem": "não vou pagar não, tá caro demais isso aí",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "lazaro"}},
    },
    "B12": {
        "nome": "Billing — lead envia comprovante por texto (com contexto) → financeiro",
        "grupo": "billing",
        "mensagem": "acabei de fazer o pix de R$189,90, segue comprovante",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "financeiro"}},
        "expect_not_contains": ["CPF", "cpf"],
    },
    "B13": {
        "nome": "Billing — lead insiste que pagou (2a vez) → financeiro",
        "grupo": "billing",
        "mensagem": "já paguei sim, tenho o comprovante aqui",
        "context": "billing",
        "historico_extra": [
            {"role": "user", "content": "já paguei ontem"},
            {"role": "model", "content": "Verifiquei aqui e o pagamento ainda não apareceu no sistema. Tem certeza que deu certo? Se quiser tentar novamente, o link é: https://sandbox.asaas.com/i/abc123"},
        ],
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "financeiro"}},
        "expect_not_contains": ["CPF", "cpf"],
    },
    "B14": {
        "nome": "Billing — lead pede Lázaro pelo nome",
        "grupo": "billing",
        "mensagem": "quero falar com o Lázaro",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "lazaro"}},
    },
    "B15": {
        "nome": "Billing — lead quer cancelar contrato → Nathália imediato",
        "grupo": "billing",
        "mensagem": "quero cancelar meu contrato, não quero mais",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "atendimento"}},
        "expect_not_contains": ["CPF", "cpf"],
    },
    "B16": {
        "nome": "Billing — lead quer devolver equipamento → Nathália imediato",
        "grupo": "billing",
        "mensagem": "quero devolver o ar condicionado, vem buscar",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "atendimento"}},
        "expect_not_contains": ["CPF", "cpf"],
    },
    "B17": {
        "nome": "Billing — lead de outra cidade com contexto (já é cliente)",
        "grupo": "billing",
        "mensagem": "moro em Cuiabá, me manda o boleto",
        "context": "billing",
        "expect_tools": [],
        "allow_tools": ["consultar_cliente", "transferir_departamento"],
        "expect_not_contains": ["CPF", "cpf"],
    },
    "B18": {
        "nome": "Billing — lead responde 'não' seco ao disparo",
        "grupo": "billing",
        "mensagem": "não",
        "context": "billing",
        "expect_tools": [],
        "allow_tools": ["consultar_cliente", "registrar_compromisso"],
        "expect_not_contains": ["CPF", "cpf", "Aqui é a Ana", "sou a Ana", "erro"],
    },
    "B19": {
        "nome": "Billing — lead contesta valor da cobrança → cobranças",
        "grupo": "billing",
        "mensagem": "esse valor tá errado, não concordo com essa cobrança, quero contestar",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "allow_tools": ["consultar_cliente"],
        "expect_args": {"transferir_departamento": {"destino": "cobrancas"}},
    },
    "B21": {
        "nome": "Billing — lead diz que pagou + manda comprovante + IA não acha → financeiro",
        "grupo": "billing",
        "mensagem": "tá aqui o comprovante do pix que fiz ontem, R$189,90",
        "context": "billing",
        "historico_extra": [
            {"role": "user", "content": "já paguei"},
            {"role": "model", "content": "Verifiquei aqui e o pagamento ainda não apareceu no sistema. Tem certeza que deu certo? Se quiser tentar novamente, o link é: https://sandbox.asaas.com/i/abc123"},
            {"role": "user", "content": "paguei sim, vou te mandar o comprovante"},
            {"role": "model", "content": "Pode mandar!"},
        ],
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "financeiro"}},
        "expect_not_contains": ["CPF", "cpf"],
    },
    "B20": {
        "nome": "Billing — lead com CPF vinculado pede segunda via (sem dar CPF de novo)",
        "grupo": "billing",
        "mensagem": "me manda a segunda via do boleto",
        "context": "billing",
        "expect_tools": [],
        "allow_tools": ["consultar_cliente"],
        "expect_contains": ["sandbox.asaas.com"],
        "expect_not_contains": ["CPF", "cpf"],
    },

    # ── Manutenção — manutenção preventiva ──
    "M1": {
        "nome": "Manutenção — lead responde com dia/hora → avisa equipe (sem tool)",
        "grupo": "manutencao",
        "mensagem": "Pode ser segunda de manhã",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender"],
        "expect_not_contains": [
            "CPF", "cpf",
            "Aqui é a Ana", "sou a Ana", "Aluga-Ar!",
            "transferir",
        ],
    },
    "M2": {
        "nome": "Manutenção — lead diz 'bom dia' ao disparo → avisa equipe (sem tool)",
        "grupo": "manutencao",
        "mensagem": "Bom dia",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender", "manutenção"],
        "expect_not_contains": ["CPF", "cpf", "transferir"],
    },
    "M3": {
        "nome": "Manutenção — lead recusa → aceita sem insistir (sem tool)",
        "grupo": "manutencao",
        "mensagem": "Não preciso de manutenção, tá tudo ok",
        "context": "manutencao",
        "expect_tools": [],
        "expect_not_contains": ["CPF", "cpf", "transferir", "insist"],
    },
    "M4": {
        "nome": "Manutenção — pergunta se é pago",
        "grupo": "manutencao",
        "mensagem": "Quanto custa a manutenção?",
        "context": "manutencao",
        "expect_contains_any": ["gratuita", "inclusa", "grátis", "sem custo", "gratuito", "incluso"],
        "expect_not_contains": ["CPF"],
    },

    "M5": {
        "nome": "Manutenção — lead diz 'quero agendar' → avisa equipe (sem tool)",
        "grupo": "manutencao",
        "mensagem": "quero agendar",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender"],
        "expect_not_contains": ["CPF", "cpf", "qual dia", "qual horário", "transferir"],
    },
    "M6": {
        "nome": "Manutenção — lead diz só 'sim' ao disparo → avisa equipe (sem tool)",
        "grupo": "manutencao",
        "mensagem": "sim",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender", "agendar"],
        "expect_not_contains": ["CPF", "cpf", "Aqui é a Ana", "sou a Ana", "transferir"],
    },
    "M7": {
        "nome": "Manutenção — lead responde com áudio genérico → avisa equipe (sem tool)",
        "grupo": "manutencao",
        "mensagem": "[mensagem de áudio]",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender"],
        "expect_not_contains": ["CPF", "cpf", "erro", "transferir"],
    },
    "M8": {
        "nome": "Manutenção — lead muda assunto para cobrança",
        "grupo": "manutencao",
        "mensagem": "ah, aproveita e me manda o boleto do mês",
        "context": "manutencao",
        "expect_tools": [],
        "allow_tools": ["consultar_cliente"],
        "expect_not_contains": ["erro", "error"],
    },
    "M9": {
        "nome": "Manutenção — lead pergunta endereço (já veio no disparo)",
        "grupo": "manutencao",
        "mensagem": "qual endereço vocês vão fazer a manutenção?",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["Rua das Flores", "Flores", "123", "endereço"],
        "expect_not_contains": ["CPF", "cpf"],
    },
    "M10": {
        "nome": "Manutenção — lead quer cancelar CONTRATO → transfere (caso especial)",
        "grupo": "manutencao",
        "mensagem": "não quero mais o aluguel, quero cancelar meu contrato",
        "context": "manutencao",
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "atendimento"}},
        "expect_not_contains": ["CPF", "cpf"],
    },
    "M11": {
        "nome": "Manutenção — lead não vai estar em casa → avisa equipe (sem tool)",
        "grupo": "manutencao",
        "mensagem": "não vou estar em casa essa semana toda, só semana que vem",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender", "semana que vem"],
        "expect_not_contains": ["CPF", "cpf", "erro", "transferir"],
    },
    "M12": {
        "nome": "Manutenção — lead relata defeito urgente (ar parou) → avisa equipe (sem tool)",
        "grupo": "manutencao",
        "mensagem": "o ar parou de funcionar completamente, não liga mais",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender", "técnic"],
        "expect_not_contains": ["CPF", "cpf", "transferir"],
    },
    "M13": {
        "nome": "Manutenção — lead relata defeito SEM contexto (orgânico) → transfere imediatamente",
        "grupo": "manutencao",
        "mensagem": "tenho um ar alugado com vocês e tá pingando água dentro de casa",
        "context": None,
        "expect_tools": ["transferir_departamento"],
        "allow_tools": ["consultar_cliente"],
        "expect_not_contains": ["erro"],
    },
    "M14": {
        "nome": "Manutenção — CASO REAL Maria de Fátima: áudio confirmando → avisa equipe (sem tool)",
        "grupo": "manutencao",
        "mensagem": "[mensagem de áudio — cliente confirma horário]",
        "context": "manutencao",
        "historico_extra": [
            {"role": "user", "content": "[mensagem de áudio]"},
            {"role": "model", "content": "Oi, bom dia! Sem problemas.\n\nPodemos agendar para hoje depois das 15h? Ou se ficar melhor pra você, temos horários na quarta-feira também."},
        ],
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender"],
        "expect_not_contains": ["CPF", "cpf", "agendado", "confirmado", "15h", "quarta", "transferir"],
    },
    "M15": {
        "nome": "Manutenção — lead responde 'pode ser hoje à tarde' → avisa equipe (sem tool, sem agendar sozinha)",
        "grupo": "manutencao",
        "mensagem": "pode ser hoje à tarde",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender"],
        "expect_not_contains": ["CPF", "cpf", "agendado", "confirmado", "transferir"],
    },
    "M16": {
        "nome": "Manutenção — lead responde 'ok' ao disparo → avisa equipe (sem tool)",
        "grupo": "manutencao",
        "mensagem": "ok",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender", "dia", "horário", "agendar"],
        "expect_not_contains": ["CPF", "cpf", "Aqui é a Ana", "sou a Ana", "transferir"],
    },

    # ── Contexto — anti-saudação ──
    "C1": {
        "nome": "Contexto — 'ok' após disparo billing",
        "grupo": "contexto",
        "mensagem": "ok",
        "context": "billing",
        "expect_not_contains": [
            "Aqui é a Ana", "sou a Ana", "Aluga-Ar!",
            "Como posso te ajudar hoje", "tudo bem?",
        ],
    },
    "C2": {
        "nome": "Contexto — 'oi' após disparo manutenção",
        "grupo": "contexto",
        "mensagem": "oi",
        "context": "manutencao",
        "expect_not_contains": [
            "Aqui é a Ana", "Aluga-Ar!",
            "Como posso te ajudar hoje", "tudo bem?",
        ],
    },
    "C3": {
        "nome": "Contexto — lead novo sem disparo → saudação OK",
        "grupo": "contexto",
        "mensagem": "Oi",
        "context": None,
        "expect_tools": [],
        "expect_not_contains": ["erro", "error"],
    },

    # ── Básico ──
    "X1": {
        "nome": "Básico — saudação",
        "grupo": "basico",
        "mensagem": "Oi, tudo bem?",
        "context": None,
        "expect_tools": [],
        "expect_not_contains": ["erro", "error"],
    },
    "X2": {
        "nome": "Básico — fora de escopo",
        "grupo": "basico",
        "mensagem": "Vocês vendem ar condicionado?",
        "context": None,
        "expect_tools": [],
    },
    "X3": {
        "nome": "Básico — boleto com CPF",
        "grupo": "basico",
        "mensagem": "Quero ver meu boleto, meu CPF é 12345678901",
        "context": None,
        "expect_tools": ["consultar_cliente"],
    },
    "X4": {
        "nome": "Básico — pede humano",
        "grupo": "basico",
        "mensagem": "Quero falar com um atendente",
        "context": None,
        "expect_tools": ["transferir_departamento"],
    },

    # ── Regressão — bugs reais de produção ──
    "R1": {
        "nome": "Regressão — ar pingando sem contexto → transfere imediatamente",
        "grupo": "regressao",
        "mensagem": "minha mãe tem um ar alugado com vocês, está pingando",
        "context": None,
        "expect_tools": ["transferir_departamento"],
        "allow_tools": ["consultar_cliente"],
        "expect_not_contains": ["erro"],
    },
    "R2": {
        "nome": "Regressão — disse vou transferir mas não chamou tool",
        "grupo": "regressao",
        "mensagem": "quero falar com o financeiro",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "expect_not_contains": ["vou te transferir", "vou transferir"],
    },
    "R3": {
        "nome": "Regressão — CPF salvo com sucesso (deve usar, não confirmar)",
        "grupo": "regressao",
        "mensagem": "meu cpf é 12345678901",
        "context": None,
        "expect_not_contains": ["CPF salvo", "salvo com sucesso", "anotado o CPF"],
    },
    "R4": {
        "nome": "Regressão — CPF com cobrança (não pode dizer que não encontrou)",
        "grupo": "regressao",
        "mensagem": "quero pagar minha parcela, meu CPF é 12345678901",
        "context": None,
        "expect_tools": ["consultar_cliente"],
        "expect_not_contains": ["não encontrei", "não achei", "não localizei"],
    },
    "R5": {
        "nome": "Regressão — Ana sauda do zero respondendo disparo billing",
        "grupo": "regressao",
        "mensagem": "oi",
        "context": "billing",
        "expect_not_contains": [
            "Sou a Ana", "da Aluga Ar", "da Aluga-Ar",
            "Como posso ajudar", "tudo bem?",
        ],
    },
    "R6": {
        "nome": "Regressão — manutenção com disparo não pede CPF (avisa equipe)",
        "grupo": "regressao",
        "mensagem": "o ar está fazendo barulho",
        "context": "manutencao",
        "expect_not_contains": ["CPF", "CNPJ", "transferir"],
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender", "técnic"],
    },

    "R7": {
        "nome": "Regressão — mudança do ar → transfere atendimento",
        "grupo": "regressao",
        "mensagem": "Pará mudança do ar, que ficou de eu mandar as fotos do local. Meu CPF é 12345678901",
        "context": None,
        "expect_tools": ["transferir_departamento"],
        "allow_tools": ["consultar_cliente"],
        "expect_not_contains": ["erro"],
    },
    "R8": {
        "nome": "Regressão — ar não gela + CPF → transfere atendimento (defeito urgente)",
        "grupo": "regressao",
        "mensagem": "Não tá gelado direito, meu CPF é 12345678901",
        "context": None,
        "expect_tools": ["transferir_departamento"],
        "allow_tools": ["consultar_cliente"],
        "expect_not_contains": ["erro"],
    },

    # ── Vendas — lead interessado em alugar ──
    "V1": {
        "nome": "Vendas — pergunta preço (com ambiente)",
        "grupo": "vendas",
        "mensagem": "quanto custa o aluguel pra um quarto?",
        "context": None,
        "expect_tools": [],
        "expect_not_contains": ["CPF", "erro"],
    },
    "V2": {
        "nome": "Vendas — qual BTU para quarto 15m²",
        "grupo": "vendas",
        "mensagem": "não sei qual ar preciso, é pra um quarto de 15m²",
        "context": None,
        "expect_tools": [],
        "expect_contains_any": ["12.000", "12000", "BTU", "btu"],
        "expect_not_contains": ["CPF", "erro"],
    },
    "V3": {
        "nome": "Vendas — como funciona o contrato",
        "grupo": "vendas",
        "mensagem": "como funciona o aluguel? tem contrato?",
        "context": None,
        "expect_tools": [],
        "expect_contains_any": ["12 meses", "instalação", "manutenção", "mensalidade"],
        "expect_not_contains": ["CPF", "erro"],
    },
    "V4": {
        "nome": "Vendas — fora da área de cobertura",
        "grupo": "vendas",
        "mensagem": "atende em São Paulo?",
        "context": None,
        "expect_tools": ["transferir_departamento"],
        "expect_not_contains": ["CPF"],
    },
    "V5": {
        "nome": "Vendas — quer fechar → coleta nome e CPF",
        "grupo": "vendas",
        "mensagem": "quero alugar, pode me mandar o contrato",
        "context": None,
        "expect_tools": [],
        "expect_contains_any": ["nome", "CPF", "dados"],
        "expect_not_contains": ["erro"],
    },
    "V6": {
        "nome": "Vendas — multa por cancelamento",
        "grupo": "vendas",
        "mensagem": "se eu quiser cancelar antes do prazo tem multa?",
        "context": None,
        "expect_tools": [],
        "allow_tools": ["transferir_departamento"],
        "expect_not_contains": ["CPF", "erro", "não sei"],
    },

    # ── Multimodal — imagem e áudio ──
    "MM1": {
        "nome": "Multimodal — comprovante de pagamento (texto) → financeiro",
        "grupo": "multimodal",
        "mensagem": "acabei de pagar, segue o comprovante do pix de R$189,90",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "financeiro"}},
    },
    "MM2": {
        "nome": "Multimodal — áudio genérico",
        "grupo": "multimodal",
        "mensagem": "[mensagem de áudio]",
        "context": None,
        "expect_tools": [],
        "expect_not_contains": ["erro", "error"],
    },

    # ── Edge cases ──
    "E1": {
        "nome": "Edge — mensagem confusa/incompleta",
        "grupo": "edge",
        "mensagem": "oi sim aquele negócio lá",
        "context": None,
        "expect_tools": [],
        "expect_not_contains": ["erro", "error"],
    },
    "E2": {
        "nome": "Edge — lead quer alugar e já deu CPF → coleta nome",
        "grupo": "edge",
        "mensagem": "quero alugar, meu CPF é 12345678901",
        "context": None,
        "expect_tools": [],
        "expect_contains_any": ["nome", "completo"],
        "expect_not_contains": ["erro"],
        "allow_tools": ["transferir_departamento", "consultar_cliente"],
    },
    "E3": {
        "nome": "Edge — pergunta sobre higienização (Mundia Ar)",
        "grupo": "edge",
        "mensagem": "vocês fazem higienização de ar?",
        "context": None,
        "expect_tools": [],
        "expect_contains_any": ["Mundia", "mundia", "@mundialar", "Instagram"],
        "expect_not_contains": ["CPF", "erro"],
    },

    # ── Snooze — compromisso de pagamento ──
    "S1": {
        "nome": "Snooze — lead diz 'vou pagar sexta' → registra compromisso",
        "grupo": "snooze",
        "mensagem": "vou pagar sexta-feira sem falta",
        "context": "billing",
        "expect_tools": ["registrar_compromisso"],
        "expect_not_contains": ["CPF", "cpf", "erro"],
    },
    "S2": {
        "nome": "Snooze — lead diz 'pago amanhã' → registra compromisso",
        "grupo": "snooze",
        "mensagem": "pago amanhã de manhã",
        "context": "billing",
        "expect_tools": ["registrar_compromisso"],
        "expect_not_contains": ["CPF", "cpf", "erro"],
    },
    "S3": {
        "nome": "Snooze — lead diz 'vou pagar depois' (vago) → registra compromisso",
        "grupo": "snooze",
        "mensagem": "vou pagar depois, essa semana eu resolvo",
        "context": "billing",
        "expect_tools": ["registrar_compromisso"],
        "allow_tools": ["consultar_cliente"],
        "expect_not_contains": ["CPF", "cpf", "erro"],
    },
    "S4": {
        "nome": "Snooze — lead diz 'semana que vem' → registra compromisso",
        "grupo": "snooze",
        "mensagem": "só consigo pagar semana que vem, pode ser?",
        "context": "billing",
        "expect_tools": ["registrar_compromisso"],
        "expect_not_contains": ["CPF", "cpf"],
    },
    "S5": {
        "nome": "Snooze — lead com snooze manda msg pedindo link → Ana responde normal",
        "grupo": "snooze",
        "mensagem": "manda o link de novo por favor",
        "context": "billing",
        "historico_extra": [
            {"role": "user", "content": "vou pagar sexta sem falta"},
            {"role": "model", "content": "Combinado! O link para pagamento é: https://sandbox.asaas.com/i/abc123"},
        ],
        "expect_contains": ["sandbox.asaas.com"],
        "expect_not_contains": ["CPF", "cpf"],
    },
    "S6": {
        "nome": "Snooze — lead quer negociar → financeiro (sem snooze)",
        "grupo": "snooze",
        "mensagem": "não tenho como pagar esse valor, preciso negociar parcelas",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "financeiro"}},
    },
    "S7": {
        "nome": "Snooze — lead SEM contexto diz 'pago sexta' → pede CPF (sem snooze)",
        "grupo": "snooze",
        "mensagem": "vou pagar sexta, me manda o boleto",
        "context": None,
        "expect_tools": [],
        "allow_tools": ["consultar_cliente", "registrar_compromisso"],
        "expect_contains_any": ["CPF", "cpf", "identificar", "localizar", "cadastro"],
        "expect_not_contains": ["erro"],
    },
    "S8": {
        "nome": "Snooze — lead responde 'ok' ao disparo → NÃO registra compromisso",
        "grupo": "snooze",
        "mensagem": "ok",
        "context": "billing",
        "expect_tools": [],
        "allow_tools": ["consultar_cliente", "registrar_compromisso"],
        "expect_not_contains": ["CPF", "cpf", "erro"],
    },

    # =========================================================================
    # TT: Tool-as-Text — valida que Gemini chama tool via function calling,
    #     NÃO escrevendo o nome da tool como texto para o cliente.
    #     Bug real: lead 556699198912 recebeu "transferir_departamento(queue_id=453, user_id=815)"
    # =========================================================================
    "TT1": {
        "nome": "Tool-as-text — limpeza de ar → transfere (não escreve tool como texto)",
        "grupo": "tool_text",
        "mensagem": "Tenho um ar condicionado em casa preciso fazer uma limpeza ar de 12 mil btus inverter",
        "context": None,
        "expect_tools": [],
        "allow_tools": ["transferir_departamento"],
        "expect_not_contains": [
            "transferir_departamento", "queue_id", "user_id",
            "consultar_cliente", "registrar_compromisso",
        ],
    },
    "TT2": {
        "nome": "Tool-as-text — ar pingando → transfere (não escreve tool como texto)",
        "grupo": "tool_text",
        "mensagem": "Meu ar tá pingando água, o que faço?",
        "context": None,
        "expect_tools": [],
        "allow_tools": ["transferir_departamento"],
        "expect_not_contains": [
            "transferir_departamento", "queue_id", "user_id",
            "consultar_cliente", "registrar_compromisso",
        ],
    },
    "TT3": {
        "nome": "Tool-as-text — cancelar contrato → transfere (não escreve tool como texto)",
        "grupo": "tool_text",
        "mensagem": "Quero cancelar meu contrato",
        "context": None,
        "expect_tools": [],
        "allow_tools": ["transferir_departamento"],
        "expect_not_contains": [
            "transferir_departamento", "queue_id", "user_id",
            "consultar_cliente", "registrar_compromisso",
        ],
    },
    "TT4": {
        "nome": "Tool-as-text — já paguei → transfere financeiro (não escreve tool como texto)",
        "grupo": "tool_text",
        "mensagem": "Já paguei o boleto",
        "context": None,
        "expect_tools": ["transferir_departamento"],
        "expect_not_contains": [
            "transferir_departamento(", "queue_id=", "user_id=",
            "consultar_cliente(", "registrar_compromisso(",
        ],
    },
    "TT5": {
        "nome": "Tool-as-text — falar com humano → transfere (não escreve tool como texto)",
        "grupo": "tool_text",
        "mensagem": "Quero falar com uma pessoa de verdade",
        "context": None,
        "expect_tools": ["transferir_departamento"],
        "expect_not_contains": [
            "transferir_departamento(", "queue_id=", "user_id=",
            "consultar_cliente(", "registrar_compromisso(",
        ],
    },
    "TT6": {
        "nome": "Tool-as-text — cidade fora da área → transfere (não escreve tool como texto)",
        "grupo": "tool_text",
        "mensagem": "Vocês atendem em Cuiabá?",
        "context": None,
        "expect_tools": [],
        "allow_tools": ["transferir_departamento"],
        "expect_not_contains": [
            "transferir_departamento", "queue_id", "user_id",
            "não atendemos", "não cobrimos",
        ],
    },
    "TT7": {
        "nome": "Tool-as-text — billing pagou → transfere (não escreve tool como texto)",
        "grupo": "tool_text",
        "mensagem": "Fiz o pix agora",
        "context": "billing",
        "expect_tools": ["transferir_departamento"],
        "expect_not_contains": [
            "transferir_departamento(", "queue_id=", "user_id=",
            "consultar_cliente(",
        ],
    },
    "TT8": {
        "nome": "Tool-as-text — defeito em contexto manutenção → avisa equipe (sem tool, sem texto de tool)",
        "grupo": "tool_text",
        "mensagem": "O ar parou de funcionar, não liga mais",
        "context": "manutencao",
        "expect_tools": [],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender", "técnic"],
        "expect_not_contains": [
            "transferir_departamento", "queue_id", "user_id",
            "transferir",
        ],
    },

    # ── Multi-turno — cenários com histórico longo ──
    "MT1": {
        "nome": "Multi-turno — funil vendas: nome → CPF → transfere",
        "grupo": "multi_turno",
        "mensagem": "João da Silva",
        "context": None,
        "historico_extra": [
            {"role": "user", "content": "quero alugar um ar condicionado"},
            {"role": "model", "content": "Que legal! Pra eu te ajudar melhor, me diz: é pra quantos ambientes?"},
            {"role": "user", "content": "só um quarto de 15m²"},
            {"role": "model", "content": "Pra um quarto de 15m², o ideal é um Split de 12.000 BTUs. O aluguel fica R$ 189,90/mês com instalação inclusa. Quer seguir? Preciso do seu nome completo e CPF."},
        ],
        "expect_tools": [],
        "expect_contains_any": ["CPF", "cpf"],
        "expect_not_contains": ["erro", "transferir"],
    },
    "MT2": {
        "nome": "Multi-turno — billing: pagou 2x insiste → financeiro",
        "grupo": "multi_turno",
        "mensagem": "já falei que paguei, olha o comprovante",
        "context": "billing",
        "historico_extra": [
            {"role": "user", "content": "já paguei ontem"},
            {"role": "model", "content": "Verifiquei aqui e o pagamento ainda não apareceu no sistema. Pode me mandar o comprovante?"},
            {"role": "user", "content": "fiz o pix de R$189,90 ontem à noite"},
            {"role": "model", "content": "Entendi! Vou verificar novamente no sistema."},
        ],
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "financeiro"}},
        "expect_not_contains": ["CPF", "cpf"],
    },
    "MT3": {
        "nome": "Multi-turno — defeito com CPF → transfere",
        "grupo": "multi_turno",
        "mensagem": "12345678901",
        "context": None,
        "historico_extra": [
            {"role": "user", "content": "meu ar parou de funcionar"},
            {"role": "model", "content": "Que chato! Vou te ajudar com isso. Me passa seu CPF pra eu localizar seu cadastro."},
        ],
        "expect_tools": ["transferir_departamento"],
        "allow_tools": ["consultar_cliente"],
        "expect_not_contains": ["erro"],
    },

    # =========================================================================
    # RG: Regressão — bugs do audit 26-28/04/2026
    # Cada cenário cobre 1 bug específico. P4 (resposta ".") não é testável
    # pelo simulador (filtro vive em processar_mensagens, fora do graph.ainvoke).
    # =========================================================================
    "RG1": {
        "nome": "Regressão P1 — NÃO inventar regra (ex: 'precisa pagar antes de instalar')",
        "grupo": "regressao",
        "mensagem": "Queria ver sobre a instalação, meu CPF é 12345678901",
        "context": None,
        "expect_tools": ["consultar_cliente"],
        "allow_tools": ["transferir_departamento"],
        "expect_not_contains": [
            "precisa pagar", "quitar antes", "regularizar antes",
            "pagar antes de instalar", "pendência", "precisa estar em dia",
            "em aberto", "quitar", "regularizar",
        ],
        "mock_overrides": {
            "cobrancas_pendentes": [{
                "id": "pay_overdue1",
                "value": 189.90,
                "due_date": "2026-04-20",
                "status": "OVERDUE",
                "invoice_url": "https://sandbox.asaas.com/i/overdue1",
            }],
        },
    },
    "RG2": {
        "nome": "Regressão P2 — NÃO agendar manutenção (transfere sem confirmar horário)",
        "grupo": "regressao",
        "mensagem": "Pode ser amanhã às 14h?",
        "context": "manutencao",
        "expect_tools": [],
        "allow_tools": ["transferir_departamento"],
        "expect_contains_any": ["equipe", "entrar em contato", "vai te atender", "vai atender", "agendar"],
        "expect_not_contains": [
            "Agendado", "agendado", "confirmado", "Confirmado",
            "14h", "amanhã às 14", "nosso técnico vai passar",
            "marcado", "combinado para amanhã",
        ],
    },
    "RG3": {
        "nome": "Regressão P3 — snooze expirado NÃO é compromisso ativo",
        "grupo": "regressao",
        "mensagem": "Oi, é sobre a mensalidade",
        "context": "billing",
        "expect_tools": [],
        "allow_tools": ["consultar_cliente"],
        "expect_not_contains": [
            "combinado dia 25", "dia 25/04", "25 de abril",
            "pagamento agendado", "compromisso ativo",
            "silenciadas até 25", "snooze",
        ],
        "mock_overrides": {
            "leads_data": [{
                "telefone": "5566999881234",
                "billing_snooze_until": "2026-04-25",
            }],
        },
    },
    "RG5": {
        "nome": "Regressão P5 — timeout na transferência → NÃO diz 'indisponível'",
        "grupo": "regressao",
        "mensagem": "quero falar com a Nathália",
        "context": None,
        "expect_tools": ["transferir_departamento"],
        "expect_not_contains": [
            "não está disponível", "indisponível", "no momento",
            "tente mais tarde", "não consegui transferir",
            "Nathália não", "atendente não",
        ],
        "mock_overrides": {
            "leadbox_side_effects": ["timeout", "success"],
        },
    },
    "RG6": {
        "nome": "Regressão P6 — múltiplas cobranças → enviar TODOS os links",
        "grupo": "regressao",
        "mensagem": "Me manda o boleto",
        "context": "billing",
        "expect_tools": [],
        "allow_tools": ["consultar_cliente"],
        "expect_contains": ["sandbox.asaas.com/i/overdue1", "sandbox.asaas.com/i/overdue2"],
        "expect_not_contains": ["mais recente", "último boleto"],
        "mock_overrides": {
            "cobrancas_pendentes": [
                {
                    "id": "pay_overdue1",
                    "value": 189.90,
                    "due_date": "2026-04-03",
                    "status": "OVERDUE",
                    "invoice_url": "https://sandbox.asaas.com/i/overdue1",
                },
                {
                    "id": "pay_overdue2",
                    "value": 189.90,
                    "due_date": "2026-03-03",
                    "status": "OVERDUE",
                    "invoice_url": "https://sandbox.asaas.com/i/overdue2",
                },
            ],
        },
    },
    "RG7": {
        "nome": "Regressão P7 — NÃO anunciar transferência (nome+CPF → transfere silencioso)",
        "grupo": "regressao",
        "mensagem": "João Silva, 123.456.789-00",
        "context": None,
        "historico_extra": [
            {"role": "user", "content": "quero alugar um ar condicionado pra um quarto de 12m²"},
            {"role": "model", "content": "Pra um quarto de 12m², o ideal é um Split de 12.000 BTUs. O aluguel fica R$ 189/mês, com instalação e manutenção inclusas no contrato de 12 meses. Quer seguir? Me passa seu nome completo e CPF!"},
        ],
        "expect_tools": ["transferir_departamento"],
        "expect_args": {"transferir_departamento": {"destino": "atendimento"}},
        "expect_not_contains": [
            "vou transferir", "já te encaminho", "já estou te transferindo",
            "já direcionei", "já te passei", "vou te passar",
            "Nathália vai te ajudar", "a Nathália", "encaminhei para",
            "direcionei para", "te encaminhando", "transferindo",
        ],
    },
}


# =============================================================================
# SUPABASE MOCK
# =============================================================================

class MockSupabaseChain:
    """Mock de query chain do Supabase (select().eq().in_().execute())."""

    def __init__(self, data=None):
        self._data = data or []

    def select(self, *args, **kwargs):
        return self

    def eq(self, col, val):
        # Filter by column
        if col == "cpf_cnpj" and self._data:
            filtered = [d for d in self._data if d.get("cpf_cnpj") == val]
            return MockSupabaseChain(filtered)
        if col == "customer_id" and self._data:
            filtered = [d for d in self._data if d.get("customer_id") == val]
            return MockSupabaseChain(filtered)
        if col == "status" and self._data:
            filtered = [d for d in self._data if d.get("status") == val]
            return MockSupabaseChain(filtered)
        if col == "telefone" and self._data:
            filtered = [d for d in self._data if d.get("telefone") == val]
            return MockSupabaseChain(filtered)
        return self

    def ilike(self, col, pattern):
        # Simula busca por telefone
        search = pattern.strip("%")
        if self._data and col == "mobile_phone":
            filtered = [d for d in self._data if search in d.get("mobile_phone", "")]
            return MockSupabaseChain(filtered)
        return self

    def in_(self, col, values):
        if self._data and col == "status":
            filtered = [d for d in self._data if d.get("status") in values]
            return MockSupabaseChain(filtered)
        return self

    def update(self, data):
        return self

    def is_(self, col, val):
        return self

    def gte(self, col, val):
        return self

    def lte(self, col, val):
        return self

    def gt(self, col, val):
        return self

    def insert(self, data):
        return self

    def order(self, col, **kwargs):
        return self

    def limit(self, n):
        self._data = self._data[:n]
        return self

    def execute(self):
        return MagicMock(data=self._data)


class MockSupabaseClient:
    """Mock completo do Supabase para consultar_cliente."""

    def __init__(self, customer=None, cobrancas_pendentes=None, cobrancas_pagas=None, contratos=None, leads_data=None):
        self._customer = customer
        self._cobrancas_pendentes = cobrancas_pendentes or []
        self._cobrancas_pagas = cobrancas_pagas or []
        self._contratos = contratos or []
        self._leads_data = leads_data or []

    def table(self, name):
        if name == "asaas_clientes":
            return MockSupabaseChain([self._customer] if self._customer else [])
        if name == "asaas_cobrancas":
            # Combine pendentes e pagas; a chain filtra por status
            all_cobs = []
            for c in self._cobrancas_pendentes:
                all_cobs.append({**c, "customer_id": self._customer["id"] if self._customer else ""})
            for c in self._cobrancas_pagas:
                all_cobs.append({
                    **c,
                    "customer_id": self._customer["id"] if self._customer else "",
                    "status": c.get("status", "RECEIVED"),
                })
            return MockSupabaseChain(all_cobs)
        if name == "asaas_contratos":
            return MockSupabaseChain([
                {**ct, "customer_id": self._customer["id"] if self._customer else "", "status": "ACTIVE"}
                for ct in self._contratos
            ])
        if name == "ana_leads":
            return MockSupabaseChain(self._leads_data)
        return MockSupabaseChain([])


def make_supabase_mock(customer=None, cobrancas_pendentes=None, cobrancas_pagas=None, contratos=None, leads_data=None):
    """Cria mock do Supabase para consultar_cliente."""
    mock_client = MockSupabaseClient(
        customer=customer or MOCK_CUSTOMER,
        cobrancas_pendentes=cobrancas_pendentes or MOCK_COBRANCAS_PENDENTES,
        cobrancas_pagas=cobrancas_pagas or MOCK_COBRANCAS_PAGAS,
        contratos=contratos or MOCK_CONTRATOS,
        leads_data=leads_data,
    )
    return patch("infra.supabase.get_supabase", return_value=mock_client)


# =============================================================================
# LEADBOX MOCK
# =============================================================================

def make_leadbox_mock():
    """Mock de httpx.Client para transferir_departamento. Retorna (patcher, mock_client)."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.post = MagicMock(return_value=mock_response)
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    patcher = patch("core.tools.httpx.Client", return_value=mock_client)
    return patcher, mock_client


def make_leadbox_mock_stateful(side_effects):
    """Mock do Leadbox com side_effect sequencial (ex: ['timeout', 'success']).

    Usado para testar retry: primeira chamada falha, segunda sucede.
    Patcha infra.leadbox_client._get_http_client (caminho real usado pela tool).
    """
    import httpx as _httpx
    call_count = {"n": 0}

    def _mock_post(*args, **kwargs):
        idx = call_count["n"]
        call_count["n"] += 1
        effect = side_effects[min(idx, len(side_effects) - 1)]
        if effect == "timeout":
            raise _httpx.TimeoutException("Mock timeout")
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        return resp

    mock_client = MagicMock()
    mock_client.post = _mock_post

    patcher = patch("infra.leadbox_client._get_http_client", return_value=mock_client)
    return patcher, mock_client


# =============================================================================
# SIMULATION RESULT
# =============================================================================

@dataclass
class SimulationResult:
    scenario_id: str
    scenario_name: str
    mensagem: str = ""
    context_type: str = ""
    tool_calls: list = field(default_factory=list)
    resposta: str = ""
    raw_messages: list = field(default_factory=list)
    validations: list = field(default_factory=list)
    passed: bool = False
    duration_ms: int = 0
    error: str = ""

    def add_validation(self, name: str, passed: bool, detail: str = ""):
        self.validations.append({"name": name, "passed": passed, "detail": detail})

    @property
    def all_passed(self) -> bool:
        if self.error:
            return False
        if not self.validations:
            return True
        return all(v["passed"] for v in self.validations)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "mensagem": self.mensagem,
            "context_type": self.context_type,
            "tool_calls": self.tool_calls,
            "resposta": self.resposta[:500],
            "raw_messages": self.raw_messages,
            "validations": self.validations,
            "passed": self.all_passed,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }

    def print_report(self):
        status = "PASS" if self.all_passed else "FAIL"
        icon = "\u2705" if self.all_passed else "\u274c"
        print(f"\n[{self.scenario_id}] {self.scenario_name}")
        print(f"  Mensagem: \"{self.mensagem}\"")
        if self.context_type:
            print(f"  Contexto: {self.context_type}")
        if self.tool_calls:
            for tc in self.tool_calls:
                args_str = ", ".join(f'{k}="{v}"' for k, v in tc.get("args", {}).items())
                print(f"  Tool call: {tc['name']}({args_str})")
        else:
            print("  Tool calls: nenhum")
        print(f"  Resposta: {self.resposta[:200]}{'...' if len(self.resposta) > 200 else ''}")
        print(f"  Valida\u00e7\u00f5es:")
        for v in self.validations:
            vi = "\u2705" if v["passed"] else "\u274c"
            detail = f" \u2014 {v['detail']}" if v["detail"] else ""
            print(f"    {vi} {v['name']}{detail}")
        if self.error:
            print(f"  \u26a0\ufe0f  Erro: {self.error}")
        print(f"  Tempo: {self.duration_ms}ms")
        print(f"  Resultado: {icon} {status}")


# =============================================================================
# ENGINE
# =============================================================================

async def run_scenario(scenario_id: str, scenario: dict) -> SimulationResult:
    """Executa um cenário de simulação."""
    result = SimulationResult(scenario_id, scenario["nome"])
    result.mensagem = scenario["mensagem"]
    result.context_type = scenario.get("context") or ""

    t0 = time.time()

    try:
        from core.grafo import graph, _context_extra
        from core.context_detector import build_context_prompt

        # 1. Injetar contexto de disparo via _context_extra (lido por call_model)
        # call_model() reconstrói o SystemMessage internamente, então NÃO passamos SystemMessage.
        # Em vez disso, setamos _context_extra[phone] que call_model concatena ao prompt.
        ctx = scenario.get("context")
        phone = LEAD["telefone"]
        _context_extra.pop(phone, None)

        if ctx == "billing":
            _context_extra[phone] = {
                "type": "billing",
                "prompt": build_context_prompt("billing", "pay_abc123"),
            }
        elif ctx == "manutencao":
            _context_extra[phone] = {
                "type": "manutencao",
                "prompt": build_context_prompt("manutencao", "contract_xyz"),
            }

        # 2. Montar histórico (SEM SystemMessage — call_model adiciona)
        messages = []

        # Histórico de disparo (billing ou manutenção)
        if ctx == "billing":
            for m in _billing_history()["messages"]:
                messages.append(AIMessage(content=m["content"]))
        elif ctx == "manutencao":
            for m in _manutencao_history()["messages"]:
                messages.append(AIMessage(content=m["content"]))

        # Histórico extra (para cenários multi-turno como M2)
        for msg in scenario.get("historico_extra", []):
            if msg["role"] == "user":
                messages.append(HumanMessage(content=msg["content"]))
            elif msg["role"] == "model":
                messages.append(AIMessage(content=msg["content"]))

        # 3. Mensagem atual do lead
        messages.append(HumanMessage(content=scenario["mensagem"]))

        # 4. Rodar grafo com mocks (suporta overrides por cenário via mock_overrides)
        mock_ov = scenario.get("mock_overrides", {})
        if "leadbox_side_effects" in mock_ov:
            leadbox_patcher, leadbox_mock = make_leadbox_mock_stateful(mock_ov["leadbox_side_effects"])
        else:
            leadbox_patcher, leadbox_mock = make_leadbox_mock()
        supabase_mock = make_supabase_mock(
            customer=mock_ov.get("customer"),
            cobrancas_pendentes=mock_ov.get("cobrancas_pendentes"),
            cobrancas_pagas=mock_ov.get("cobrancas_pagas"),
            contratos=mock_ov.get("contratos"),
            leads_data=mock_ov.get("leads_data"),
        )
        with supabase_mock:
            with leadbox_patcher:
                invoke_result = await graph.ainvoke(
                    {"messages": messages, "phone": phone},
                )

        result.duration_ms = int((time.time() - t0) * 1000)

        # 6. Extrair tool calls
        all_messages = invoke_result.get("messages", [])
        for msg in all_messages:
            if isinstance(msg, AIMessage) and msg.tool_calls:
                for tc in msg.tool_calls:
                    result.tool_calls.append({
                        "name": tc["name"],
                        "args": tc.get("args", {}),
                    })

        # 7. Extrair resposta final (última AIMessage com texto)
        for msg in reversed(all_messages):
            if isinstance(msg, AIMessage) and msg.content:
                content = msg.content
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    )
                if content.strip():
                    result.resposta = content.strip()
                    break

        # 7b. Serializar AIMessages brutos para evidência
        for msg in all_messages:
            if isinstance(msg, AIMessage):
                raw = {
                    "type": "ai",
                    "content": msg.content[:500] if isinstance(msg.content, str) else str(msg.content)[:500],
                    "tool_calls": [{"name": tc["name"], "args": tc.get("args", {})} for tc in (msg.tool_calls or [])],
                }
                if hasattr(msg, "response_metadata") and msg.response_metadata:
                    raw["finish_reason"] = msg.response_metadata.get("finish_reason", "")
                result.raw_messages.append(raw)
            elif isinstance(msg, ToolMessage):
                result.raw_messages.append({
                    "type": "tool",
                    "name": msg.name if hasattr(msg, "name") else "",
                    "content": str(msg.content)[:300],
                })

        # 8. Validar
        _validate(result, scenario)

    except Exception as e:
        result.duration_ms = int((time.time() - t0) * 1000)
        result.error = f"{type(e).__name__}: {str(e)}"
        logger.exception(f"[{scenario_id}] Erro na simula\u00e7\u00e3o")

    result.passed = result.all_passed
    return result


async def run_scenario_with_retries(scenario_id: str, scenario: dict, retries: int = 1) -> SimulationResult:
    """Wrapper com majority vote: roda até `retries` vezes, PASS se maioria passou."""
    results = []
    for attempt in range(retries):
        result = await run_scenario(scenario_id, scenario)
        results.append(result)
        if result.all_passed:
            break  # no need to retry on pass

    # Pick best result (first passing, or first)
    best = results[0]
    for r in results:
        if r.all_passed:
            best = r
            break

    # Majority vote validation
    if retries > 1 and len(results) > 1:
        pass_count = sum(1 for r in results if r.all_passed)
        best.validations.append({
            "name": f"Majority vote ({pass_count}/{len(results)} runs passed)",
            "passed": pass_count > len(results) // 2,
            "detail": f"{len(results)} tentativas" if len(results) > 1 else "",
        })

    best.passed = best.all_passed
    return best


def _validate(result: SimulationResult, scenario: dict):
    """Executa validações contra o resultado."""
    tool_names = [tc["name"] for tc in result.tool_calls]

    # Tools esperadas
    expected_tools = scenario.get("expect_tools")
    if expected_tools is not None:
        if expected_tools:
            for tool_name in expected_tools:
                found = tool_name in tool_names
                result.add_validation(
                    f"Tool {tool_name} chamada",
                    found,
                    f"chamadas: {tool_names}" if not found else "",
                )
        else:
            # Lista vazia = nenhuma tool esperada
            allowed = scenario.get("allow_tools", [])
            forbidden = [t for t in tool_names if t not in allowed]
            no_tools = len(forbidden) == 0
            result.add_validation(
                "Nenhuma tool chamada" + (f" (permitidas: {allowed})" if allowed else ""),
                no_tools,
                f"chamadas: {forbidden}" if not no_tools else "",
            )

    # Args esperados
    for tool_name, expected_args in scenario.get("expect_args", {}).items():
        matching_calls = [tc for tc in result.tool_calls if tc["name"] == tool_name]
        if matching_calls:
            actual_args = matching_calls[0]["args"]
            for key, expected_val in expected_args.items():
                actual_val = str(actual_args.get(key, ""))
                match = expected_val.lower() in actual_val.lower()
                result.add_validation(
                    f"Arg {tool_name}.{key} cont\u00e9m '{expected_val}'",
                    match,
                    f"valor real: '{actual_val}'" if not match else "",
                )

    # Resposta contém
    for text in scenario.get("expect_contains", []):
        found = text.lower() in result.resposta.lower()
        result.add_validation(
            f"Resposta cont\u00e9m '{text}'",
            found,
            f"resposta: {result.resposta[:100]}" if not found else "",
        )

    # Resposta contém qualquer um (OR)
    any_list = scenario.get("expect_contains_any", [])
    if any_list:
        found = any(t.lower() in result.resposta.lower() for t in any_list)
        result.add_validation(
            f"Resposta cont\u00e9m algum de {any_list}",
            found,
            f"resposta: {result.resposta[:100]}" if not found else "",
        )

    # Resposta NÃO contém
    for text in scenario.get("expect_not_contains", []):
        not_found = text.lower() not in result.resposta.lower()
        result.add_validation(
            f"Resposta N\u00c3O cont\u00e9m '{text}'",
            not_found,
            f"mas cont\u00e9m!" if not not_found else "",
        )


# =============================================================================
# MAIN
# =============================================================================

async def main():
    parser = argparse.ArgumentParser(description="Lead Simulator \u2014 testa a Ana end-to-end")
    parser.add_argument("scenario_id", nargs="?", help="ID do cen\u00e1rio (ex: B1, M2)")
    parser.add_argument("--group", help="Rodar grupo (billing, manutencao, snooze, regressao, vendas, basico, contexto, edge, multimodal)")
    parser.add_argument("--all", action="store_true", help="Rodar todos os cen\u00e1rios")
    parser.add_argument("--report", action="store_true", help="Gerar relat\u00f3rio JSON")
    parser.add_argument("--adhoc", help="Mensagem ad-hoc para testar")
    parser.add_argument("--context", help="Contexto para ad-hoc: billing ou manutencao")
    parser.add_argument("--expect-tool", help="Tool esperada (ad-hoc)")
    parser.add_argument("--expect-no-tool", action="store_true", help="Nenhuma tool esperada (ad-hoc)")
    parser.add_argument("--expect-contains", help="Resposta deve conter (ad-hoc)")
    parser.add_argument("--expect-not-contains", help="Resposta N\u00c3O deve conter (ad-hoc)")
    parser.add_argument("--retries", type=int, default=1, help="Tentativas por cen\u00e1rio (majority vote, max 3)")

    args = parser.parse_args()
    args.retries = max(1, min(3, args.retries))

    # Ad-hoc
    if args.adhoc:
        scenario = {
            "nome": f"Ad-hoc: {args.adhoc[:50]}",
            "mensagem": args.adhoc,
            "context": args.context,
        }
        if args.expect_tool:
            scenario["expect_tools"] = [args.expect_tool]
        elif args.expect_no_tool:
            scenario["expect_tools"] = []
        if args.expect_contains:
            scenario["expect_contains"] = [args.expect_contains]
        if args.expect_not_contains:
            scenario["expect_not_contains"] = [args.expect_not_contains]

        result = await run_scenario_with_retries("ADHOC", scenario, args.retries)
        result.print_report()
        return

    # Selecionar cenários
    if args.all:
        to_run = list(SCENARIOS.items())
    elif args.group:
        to_run = [(k, v) for k, v in SCENARIOS.items() if v.get("grupo") == args.group]
        if not to_run:
            print(f"Grupo '{args.group}' n\u00e3o encontrado. Dispon\u00edveis: billing, manutencao, contexto, basico")
            return
    elif args.scenario_id:
        sid = args.scenario_id.upper()
        if sid not in SCENARIOS:
            print(f"Cen\u00e1rio '{sid}' n\u00e3o encontrado. Dispon\u00edveis: {', '.join(SCENARIOS.keys())}")
            return
        to_run = [(sid, SCENARIOS[sid])]
    else:
        parser.print_help()
        return

    # Executar
    results = []
    for sid, scenario in to_run:
        result = await run_scenario_with_retries(sid, scenario, args.retries)
        result.print_report()
        results.append(result)

    # Resumo
    total = len(results)
    passed = sum(1 for r in results if r.all_passed)
    failed = total - passed
    print(f"\n{'='*60}")
    print(f"TOTAL: {total} | \u2705 PASS: {passed} | \u274c FAIL: {failed}")
    print(f"{'='*60}")

    # Relatório JSON
    if args.report:
        report_dir = Path(__file__).parent.parent / "results"
        report_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = report_dir / f"report_{ts}.json"
        report_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total": total,
            "passed": passed,
            "failed": failed,
            "results": [r.to_dict() for r in results],
        }
        report_path.write_text(json.dumps(report_data, indent=2, ensure_ascii=False))
        print(f"\nRelat\u00f3rio salvo: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
