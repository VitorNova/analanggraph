"""
Template: Webhook WhatsApp via UAZAPI.

Baseado em: /var/www/agente-langgraph/api/webhooks/whatsapp.py (produção)

Recebe mensagens do UAZAPI → parser → buffer → grafo.
Suporta: texto, imagem, áudio, documento, comandos.

Uso:
    Copie e registre no FastAPI:
    app.include_router(router, prefix="/webhook")
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)
router = APIRouter()

from infra.redis import get_redis_service
from infra.buffer import get_message_buffer
from infra.persistencia import upsert_lead

_buffer_initialized = False


async def _init_buffer():
    """Inicializa buffer com callback de processamento (lazy)."""
    global _buffer_initialized
    if _buffer_initialized:
        return

    from core.grafo import processar_mensagens

    buffer = await get_message_buffer()
    buffer.set_process_callback(processar_mensagens)
    _buffer_initialized = True
    logger.info("[WEBHOOK] Buffer inicializado")


def extrair_mensagem(data: dict) -> Optional[dict]:
    """Parser de payload UAZAPI.

    Suporta 2 formatos:
    - Novo: EventType: "messages"
    - Antigo: event: "messages.upsert"

    Extrai: texto, imagem, áudio, documento.
    """
    from core.whatsapp.baixar_midia import baixar_imagem, baixar_audio, baixar_documento

    # Formato novo
    event_type = data.get("EventType", "")
    if event_type == "messages":
        msg = data.get("data", {}).get("message", {})
        key = data.get("data", {}).get("key", {})

        phone = key.get("remoteJid", "").replace("@s.whatsapp.net", "")
        phone = "".join(filter(str.isdigit, phone))
        from_me = key.get("fromMe", False)
        nome = data.get("data", {}).get("pushName", "")

        if not phone:
            return None

        result = {
            "telefone": phone,
            "nome": nome,
            "from_me": from_me,
        }

        # Imagem
        img_msg = msg.get("imageMessage")
        if img_msg:
            message_id = data.get("data", {}).get("key", {}).get("id", "")
            caption = img_msg.get("caption", "")
            mimetype = img_msg.get("mimetype", "image/jpeg")
            imagem_base64 = baixar_imagem(message_id) if message_id else None
            if not imagem_base64:
                imagem_base64 = img_msg.get("jpegThumbnail", "")
            result["texto"] = caption or "[Imagem enviada]"
            if imagem_base64:
                result["imagem_base64"] = imagem_base64
                result["imagem_mimetype"] = mimetype
            return result

        # Áudio
        audio_msg = msg.get("audioMessage")
        if audio_msg:
            message_id = data.get("data", {}).get("key", {}).get("id", "")
            duracao = audio_msg.get("seconds", 0)
            if message_id:
                audio_result = baixar_audio(message_id)
                if audio_result:
                    result["texto"] = f"[Áudio de {duracao}s]"
                    result["audio_base64"] = audio_result[0]
                    result["audio_mimetype"] = audio_result[1]
                    return result
            result["texto"] = "[Áudio enviado - não foi possível processar]"
            return result

        # Documento
        doc_msg = msg.get("documentMessage")
        if doc_msg:
            message_id = data.get("data", {}).get("key", {}).get("id", "")
            mimetype = doc_msg.get("mimetype", "")
            doc_name = doc_msg.get("fileName", "[Documento]")
            caption = doc_msg.get("caption", "")
            if message_id:
                doc_result = baixar_documento(message_id, mimetype)
                if doc_result:
                    base64_data, mime_limpo = doc_result
                    if mime_limpo == "application/pdf":
                        result["texto"] = caption or f"[Documento PDF: {doc_name}]"
                        result["documento_base64"] = base64_data
                        result["documento_mimetype"] = mime_limpo
                        result["documento_nome"] = doc_name
                        return result
                    if mime_limpo.startswith("image/"):
                        result["texto"] = caption or f"[Imagem: {doc_name}]"
                        result["imagem_base64"] = base64_data
                        result["imagem_mimetype"] = mime_limpo
                        return result
            result["texto"] = f"[Documento: {doc_name}]"
            return result

        # Texto
        texto = msg.get("conversation") or msg.get("extendedTextMessage", {}).get("text", "")
        result["texto"] = texto.strip() if texto else ""
        return result

    # Formato antigo
    event = data.get("event", "")
    if event == "messages.upsert":
        messages = data.get("data", [])
        if not messages:
            return None
        msg = messages[0] if isinstance(messages, list) else messages
        key = msg.get("key", {})

        phone = key.get("remoteJid", "").replace("@s.whatsapp.net", "")
        phone = "".join(filter(str.isdigit, phone))
        from_me = key.get("fromMe", False)
        texto = msg.get("message", {}).get("conversation", "")
        nome = msg.get("pushName", "")

        if not phone:
            return None

        return {
            "telefone": phone,
            "texto": texto.strip() if texto else "",
            "nome": nome,
            "from_me": from_me,
        }

    return None


async def _handle_comando(phone: str, cmd: str) -> dict:
    """Processa comandos /p, /a, /r."""
    redis = await get_redis_service()

    if cmd in ["/p", "/pausar", "/pause"]:
        await redis.pause_set(phone)
        return {"status": "ok", "action": "paused"}

    if cmd in ["/a", "/ativar", "/activate"]:
        await redis.pause_clear(phone)
        return {"status": "ok", "action": "activated"}

    if cmd in ["/r", "/reset"]:
        await redis.pause_clear(phone)
        await redis.buffer_clear(phone)
        await redis.lock_release(phone)
        return {"status": "ok", "action": "reset"}

    return {"status": "ignored", "reason": "unknown_command"}


@router.post("/uazapi")
async def webhook_whatsapp(request: Request):
    """Webhook principal — recebe mensagens do UAZAPI."""
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "reason": "invalid_json"}

    await _init_buffer()

    # 1. Parse
    msg = extrair_mensagem(body)
    if not msg or not msg.get("telefone"):
        return {"status": "ignored"}

    phone = msg["telefone"]
    texto = msg.get("texto", "")

    # 2. Comandos
    if texto.startswith("/"):
        return await _handle_comando(phone, texto.strip().lower())

    # 3. Human takeover (msg do dono → pausar IA)
    #    Não pausar se a IA está processando (evita race com resposta UAZAPI)
    if msg.get("from_me"):
        buffer = await get_message_buffer()
        if phone in buffer._processing_keys:
            logger.info(f"[WEBHOOK:{phone}] fromMe=true mas IA processando - ignorando pausa")
            return {"status": "ignored", "reason": "processing"}
        redis = await get_redis_service()
        await redis.pause_set(phone)
        logger.info(f"[WEBHOOK:{phone}] fromMe=true → IA pausada")
        return {"status": "paused"}

    # 4. Ignorar msgs vazias
    if not texto:
        return {"status": "ignored", "reason": "empty"}

    # 5. Upsert lead
    upsert_lead(phone, msg.get("nome"))

    # 6. Buffer
    buffer = await get_message_buffer()
    await buffer.add_message(phone, msg, context={"nome": msg.get("nome")})

    return {"status": "buffered"}
