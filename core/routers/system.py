"""Routes système : santé, introspection des agrégateurs, seuil d'onglets."""

import logging

from fastapi import APIRouter, HTTPException

from core import registry
from core import runtime
from core.db import db
from core.schemas.system import MaxTabsRequest

log = logging.getLogger("ai_browser2")

router = APIRouter()


@router.get("/health", tags=["system"], summary="Santé du service")
async def health():
    """Statut du service + liste des agrégateurs enregistrés."""
    return {"status": "ok", "version": "0.2", "aggregators": registry.names()}


@router.get("/aggregators", tags=["system"], summary="Agrégateurs disponibles")
async def list_aggregators():
    """Liste les agrégateurs et, pour chacun, les réseaux exacts acceptés."""
    return {
        "aggregators": [
            {"name": name, "supported_networks": registry.get(name).supported_networks}
            for name in registry.names()
        ]
    }


@router.get("/config/max-tabs", tags=["system"], summary="Seuil d'onglets par navigateur")
async def get_max_tabs():
    """Nombre max d'onglets par Chrome avant d'en lancer un nouveau (concurrence)."""
    browser = runtime.get_browser()
    pools = len(browser._browsers) if browser else 0
    return {"max_tabs": browser.max_tabs if browser else None, "open_browsers": pools}


@router.put("/config/max-tabs", tags=["system"], summary="Modifier le seuil d'onglets")
async def set_max_tabs(req: MaxTabsRequest):
    """Change le seuil (persisté en BD, appliqué immédiatement au pool en cours)."""
    browser = runtime.get_browser()
    if not browser:
        raise HTTPException(500, "Not initialized")
    await db.set_max_tabs(req.max_tabs)   # persiste (no-op si Supabase off)
    browser.max_tabs = req.max_tabs       # effet immédiat sur le pool vivant
    log.info("max_tabs_per_browser -> %d", req.max_tabs)
    return {"max_tabs": browser.max_tabs}
