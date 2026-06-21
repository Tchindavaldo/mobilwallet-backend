"""Routes système : santé, introspection des agrégateurs, seuil d'onglets."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from core import registry
from core import runtime
from core.db import db
from core.fees import fees_info
from core.schemas.system import MaxTabsRequest

log = logging.getLogger("ai_browser2")

router = APIRouter()


@router.get("/health", tags=["system"], summary="Santé du service")
async def health():
    """Statut du service + liste des agrégateurs enregistrés."""
    return {"status": "ok", "version": "0.2", "aggregators": registry.names()}


@router.get("/aggregators", tags=["system"], summary="Agrégateurs disponibles")
async def list_aggregators():
    """Liste les agrégateurs, leurs réseaux acceptés et leurs taux de frais."""
    configs = {c["name"]: c for c in await db.list_aggregator_configs()}
    return {
        "aggregators": [
            {
                "name": name,
                "supported_networks": registry.get(name).supported_networks,
                "aggregator_fee_rate": float(
                    (configs.get(name) or {}).get("aggregator_fee_rate") or 0
                ),
                "mw_commission_type": (configs.get(name) or {}).get(
                    "mw_commission_type", "percent"
                ),
                "mw_commission_value": float(
                    (configs.get(name) or {}).get("mw_commission_value") or 0
                ),
            }
            for name in registry.names()
        ]
    }


@router.get("/aggregators/{name}/fees", tags=["system"],
            summary="Détail des frais pour un agrégateur")
async def aggregator_fees(
    name: str,
    amount: Optional[int] = Query(None, description="Simuler la ventilation pour ce montant (XAF)"),
):
    """Retourne les taux de frais de l'agrégateur et, si `amount` est fourni,
    la ventilation exacte (frais agrégateur, commission MW, montant crédité à l'app).

    Utile pour les backends clients qui veulent afficher les frais à leur utilisateur.
    """
    if name not in registry.names():
        raise HTTPException(404, f"Agrégateur '{name}' inconnu")
    config = await db.get_aggregator_config(name)
    result: dict = {
        "aggregator": name,
        "aggregator_fee_rate": float((config or {}).get("aggregator_fee_rate") or 0),
        "mw_commission_type": (config or {}).get("mw_commission_type", "percent"),
        "mw_commission_value": float((config or {}).get("mw_commission_value") or 0),
    }
    if amount is not None:
        if amount <= 0:
            raise HTTPException(400, "amount doit être > 0")
        result["simulation"] = fees_info(amount, config)
    return result


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
