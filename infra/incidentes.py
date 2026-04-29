"""Registro de incidentes no Supabase — cada falha vira um registro consultável."""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def registrar_incidente(telefone: str, tipo: str, detalhe: str = "", contexto: dict = None):
    """Salva incidente na tabela ana_incidentes.

    Tipos padronizados:
        envio_falhou        — resposta da Ana não chegou ao lead
        transferencia_falhou — tool transferir_departamento deu erro HTTP
        hallucination       — Ana disse que fez algo sem chamar a tool
        gemini_falhou       — 3 tentativas de Gemini falharam
        contexto_falhou     — detecção de billing/manutenção deu erro
        snooze_falhou       — auto-snooze ou registrar_compromisso falhou
        webhook_erro        — payload inválido ou erro no handler
        media_erro          — erro ao baixar/processar mídia
    """
    try:
        from infra.supabase import get_supabase
        sb = get_supabase()
        if not sb:
            logger.warning(f"[INCIDENTE] Supabase indisponível, não registrou: {tipo} phone={telefone}")
            return

        phone_clean = "".join(filter(str.isdigit, telefone))

        sb.table("ana_incidentes").insert({
            "telefone": phone_clean,
            "tipo": tipo,
            "detalhe": detalhe[:500] if detalhe else "",
            "contexto": contexto or {},
        }).execute()

        logger.info(f"[INCIDENTE] Registrado: {tipo} phone={phone_clean}")
    except Exception as e:
        logger.warning(f"[INCIDENTE] Falha ao registrar {tipo}: {e}")
