"""Montant minimum (et maximum) d'un dépôt, par agrégateur et réseau (neutre).

Minimum = le plus grand de :
  - notre minimum : plus petit montant qui laisse au moins 1 XAF à l'app une
    fois retirés frais de dépôt, commission MobileWallet et réserve de payout
    (tous arrondis à l'excès) -> recalculé depuis la config en BD ;
  - le minimum publié par l'agrégateur (`aggregators.<nom>.account_conf`, optionnel).
Maximum = celui de l'agrégateur s'il en publie un.
`check` est appelé par /pay et /pay/page AVANT tout appel à l'agrégateur.
"""

import importlib
import logging

from fastapi import HTTPException

from core.db import db
from core.fees import compute_fees, fee_network

log = logging.getLogger("ai_browser2")

NETWORKS = ("orange", "mtn")
_SEARCH_MAX = 100_000


async def _provider_limits(aggregator: str) -> dict:
    try:
        mod = importlib.import_module(f"aggregators.{aggregator}.account_conf")
    except ModuleNotFoundError:
        return {}
    try:
        return await mod.amount_limits("DEPOSIT")
    except Exception as e:  # noqa: BLE001
        log.warning("limites %s injoignables : %s", aggregator, e)
        return {}


async def _own_minimum(aggregator: str, network: str, app_id: int | None) -> int:
    agg_config = await db.get_aggregator_config(aggregator)
    app_config = await db.get_app_commission(app_id) if app_id is not None else None
    dep = await db.get_aggregator_fee_rate(aggregator, network, "deposit")
    pay = await db.get_aggregator_fee_rate(aggregator, network, "payout")
    for gross in range(1, _SEARCH_MAX):
        if compute_fees(gross, agg_config, app_config, dep, pay).app_amount >= 1:
            return gross
    return _SEARCH_MAX


async def limits(aggregator: str, network: str | None, app_id: int | None = None) -> dict:
    """{'network', 'min', 'max', 'own_min', 'provider_min'}. Réseau inconnu (page
    de paiement, réseau choisi par le client) : le plus contraignant des réseaux."""
    net = fee_network(network)
    nets = [net] if net else list(NETWORKS)
    prov = await _provider_limits(aggregator)
    own = max([await _own_minimum(aggregator, n, app_id) for n in nets])
    pmin = max([prov.get(n, {}).get("min") or 1 for n in nets])
    maxes = [prov[n]["max"] for n in nets if prov.get(n, {}).get("max")]
    return {"network": net or "any", "min": max(own, pmin), "max": min(maxes) if maxes else None,
            "own_min": own, "provider_min": pmin, "currency": "XAF"}


async def check(aggregator: str, network: str | None, amount: int,
                app_id: int | None = None) -> None:
    """Lève 422 amount_too_low / amount_too_high hors limites."""
    lim = await limits(aggregator, network, app_id)
    if amount < lim["min"]:
        raise HTTPException(422, {
            "error": "amount_too_low", "message": f"Montant minimum : {lim['min']} F.",
            "dev_message": (f"Minimum {lim['min']} XAF = max(minimum MobileWallet "
                            f"{lim['own_min']} XAF pour qu'il reste au moins 1 XAF à l'app "
                            f"après frais, commission et réserve ; minimum agrégateur "
                            f"{lim['provider_min']} XAF)."),
            **lim})
    if lim["max"] and amount > lim["max"]:
        raise HTTPException(422, {
            "error": "amount_too_high", "message": f"Montant maximum : {lim['max']} F.",
            "dev_message": "Maximum publié par l'agrégateur pour cet opérateur.", **lim})
