"""
Template: Supabase client singleton.

Baseado em: /var/www/agente-langgraph/infra/supabase.py (produção)

Uso:
    Copie para infra/supabase.py

    from infra.supabase import get_supabase
    supabase = get_supabase()
    supabase.table("leads").select("*").execute()
"""

import logging
import os
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

logger = logging.getLogger(__name__)

_supabase_client: Optional[Client] = None


def get_supabase() -> Optional[Client]:
    """Retorna client Supabase singleton. None se não configurado."""
    global _supabase_client

    if _supabase_client is not None:
        return _supabase_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    if not url or not key:
        logger.warning("[SUPABASE] URL ou KEY não configurados")
        return None

    try:
        _supabase_client = create_client(url, key)
        logger.info("[SUPABASE] Conectado")
        return _supabase_client
    except Exception as e:
        logger.error(f"[SUPABASE] Erro: {e}")
        return None


def get_supabase_or_raise() -> Client:
    """Retorna client ou levanta ValueError."""
    client = get_supabase()
    if client is None:
        raise ValueError("Supabase não configurado. Verifique SUPABASE_URL e SUPABASE_KEY.")
    return client


def get_uazapi_config() -> dict:
    """Retorna config da UAZAPI do .env."""
    return {
        "url": os.environ.get("UAZAPI_URL", "").rstrip("/"),
        "token": os.environ.get("UAZAPI_TOKEN", ""),
        "instance": os.environ.get("UAZAPI_INSTANCE", ""),
    }
