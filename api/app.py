"""
Template: FastAPI entry point.

Baseado em: /var/www/agente-langgraph/api/app.py (produção)

Uso:
    Copie para api/app.py
    Rode: uvicorn api.app:app --port 3200 --reload
"""

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup e shutdown do app."""
    logger.info("[APP] Iniciando...")

    # Conectar Redis
    from infra.redis import get_redis_service
    await get_redis_service()

    logger.info("[APP] Pronto")
    yield
    logger.info("[APP] Encerrando...")


app = FastAPI(title="Agente IA WhatsApp", lifespan=lifespan)


# ── Routers ──
from api.webhooks.whatsapp import router as whatsapp_router
app.include_router(whatsapp_router, prefix="/webhook")

from api.webhooks.leadbox import router as leadbox_router
app.include_router(leadbox_router, prefix="/webhook")


@app.get("/")
async def root():
    return {"status": "online", "agent": "langgraph-whatsapp"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
