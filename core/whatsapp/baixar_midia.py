"""Download de mídia via UAZAPI.

Baixa imagens, áudios e documentos em base64 para envio ao Gemini.
"""

import logging
import os
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

UAZAPI_URL = os.environ.get("UAZAPI_URL", "")
UAZAPI_TOKEN = os.environ.get("UAZAPI_TOKEN", "")


def baixar_imagem(message_id: str) -> Optional[str]:
    """Baixa imagem em base64 via UAZAPI."""
    if not UAZAPI_URL or not UAZAPI_TOKEN:
        return None

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{UAZAPI_URL}/message/download",
                headers={"token": UAZAPI_TOKEN},
                json={"id": message_id, "return_base64": True, "return_link": False},
            )
            response.raise_for_status()
            data = response.json()
            base64_data = data.get("base64Data")
            if base64_data:
                logger.info(f"[MIDIA] Imagem baixada ({len(base64_data)} chars)")
                return base64_data
        return None
    except Exception as e:
        logger.error(f"[MIDIA] Erro ao baixar imagem: {e}")
        return None


def baixar_audio(message_id: str) -> Optional[Tuple[str, str]]:
    """Baixa áudio em base64 via UAZAPI. Retorna (base64, mimetype)."""
    if not UAZAPI_URL or not UAZAPI_TOKEN:
        return None

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{UAZAPI_URL}/message/download",
                headers={"token": UAZAPI_TOKEN},
                json={"id": message_id, "return_base64": True, "return_link": False},
            )
            response.raise_for_status()
            data = response.json()
            base64_data = data.get("base64Data")
            mimetype = data.get("mimetype", "audio/ogg")
            if mimetype and ";" in mimetype:
                mimetype = mimetype.split(";")[0].strip()
            if base64_data:
                logger.info(f"[MIDIA] Áudio baixado ({len(base64_data)} chars)")
                return (base64_data, mimetype)
        return None
    except Exception as e:
        logger.error(f"[MIDIA] Erro ao baixar áudio: {e}")
        return None


_MIMETYPES_SUPORTADOS = {
    "application/pdf",
    "image/jpeg", "image/png", "image/gif", "image/webp",
}


def baixar_documento(message_id: str, mimetype: str) -> Optional[Tuple[str, str]]:
    """Baixa documento em base64 via UAZAPI. Retorna (base64, mimetype_limpo)."""
    if not UAZAPI_URL or not UAZAPI_TOKEN:
        return None

    mime_limpo = mimetype.split(";")[0].strip().lower() if mimetype else ""
    if mime_limpo not in _MIMETYPES_SUPORTADOS:
        logger.info(f"[MIDIA] Documento ignorado (tipo não suportado: {mime_limpo})")
        return None

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{UAZAPI_URL}/message/download",
                headers={"token": UAZAPI_TOKEN},
                json={"id": message_id, "return_base64": True, "return_link": False},
            )
            response.raise_for_status()
            data = response.json()
            base64_data = data.get("base64Data")
            if base64_data:
                logger.info(f"[MIDIA] Documento baixado ({mime_limpo}, {len(base64_data)} chars)")
                return (base64_data, mime_limpo)
        return None
    except Exception as e:
        logger.error(f"[MIDIA] Erro ao baixar documento: {e}")
        return None
