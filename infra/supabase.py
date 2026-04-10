"""Supabase client singleton."""

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
        logger.error(f"[SUPABASE] Erro: {e}", exc_info=True)
        return None

