"""Event Logger — captura eventos estruturados para análise posterior.

Salva em logs/events.jsonl (um JSON por linha).
Cada evento tem: timestamp, tipo, phone, dados extras.

Uso:
    from infra.event_logger import log_event
    log_event("tool_call", phone, tool="consultar_cliente", args={...})
    log_event("transfer", phone, queue_id=454, user_id=814)
    log_event("snooze_set", phone, until="2026-04-06")
    log_event("response", phone, text="Sua fatura é...", duration_ms=1200)
    log_event("error", phone, error="TimeoutError", detail="Gemini timeout")
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

LOGS_DIR = Path(__file__).parent.parent / "logs"
EVENTS_FILE = LOGS_DIR / "events.jsonl"
TIMEZONE_OFFSET = -4  # UTC-4 Mato Grosso


def log_event(event_type: str, phone: str = "", **kwargs):
    """Salva evento estruturado no arquivo JSONL."""
    try:
        LOGS_DIR.mkdir(exist_ok=True)

        # Rotação: se arquivo > 5MB, arquivar e começar novo
        if EVENTS_FILE.exists() and EVENTS_FILE.stat().st_size > 5 * 1024 * 1024:
            _rotate()

        now = datetime.now(timezone(timedelta(hours=TIMEZONE_OFFSET)))
        entry = {
            "ts": now.isoformat(),
            "type": event_type,
            "phone": phone[-8:] if phone else "",  # últimos 8 dígitos (privacidade)
            **kwargs,
        }
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[EVENT_LOG] Falha ao salvar evento: {e}")


def _rotate():
    """Arquiva events.jsonl → events.YYYY-MM-DD.jsonl e limpa arquivos > 30 dias."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        archive = LOGS_DIR / f"events.{today}.jsonl"
        EVENTS_FILE.rename(archive)
        logger.info(f"[EVENT_LOG] Rotação: {EVENTS_FILE.name} → {archive.name}")

        # Limpar arquivos > 30 dias
        cutoff = datetime.now() - timedelta(days=30)
        for f in LOGS_DIR.glob("events.*.jsonl"):
            if f.stat().st_mtime < cutoff.timestamp():
                f.unlink()
                logger.info(f"[EVENT_LOG] Removido arquivo antigo: {f.name}")
    except Exception as e:
        logger.warning(f"[EVENT_LOG] Erro na rotação: {e}")
