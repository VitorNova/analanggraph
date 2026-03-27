"""Jobs de Pipeline - Executar via PM2 cron.

Marca leads como perdido baseado em regras de tempo.

Uso:
    python jobs/rotinas.py perdidos   # Roda à meia-noite
"""

import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from infra.supabase import get_supabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def marcar_perdidos():
    """Marca como perdido quem não interage há 7 dias.

    Aplica a leads em estado 'ai' (sem atendimento humano ativo).
    Executar à meia-noite diariamente.
    """
    supabase = get_supabase()
    if not supabase:
        logger.error("[PIPELINE] Supabase não configurado")
        return

    limite = (datetime.now() - timedelta(days=7)).isoformat()

    try:
        result = supabase.table("ana_leads").update({
            "current_state": "abandoned",
            "updated_at": datetime.now().isoformat(),
        }).eq(
            "current_state", "ai"
        ).lte(
            "last_interaction_at", limite
        ).execute()

        count = len(result.data) if result.data else 0
        logger.info(f"[PIPELINE] {count} leads marcados como abandonados (sem interação desde {limite[:10]})")

    except Exception as e:
        logger.exception("[PIPELINE] Falha ao marcar perdidos")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        job = sys.argv[1]
        if job == "perdidos":
            marcar_perdidos()
        else:
            print(f"Job desconhecido: {job}")
            print("Uso: python jobs/rotinas.py perdidos")
    else:
        marcar_perdidos()
