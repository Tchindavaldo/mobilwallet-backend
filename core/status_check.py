"""Vérification manuelle du statut d'une transaction chez son agrégateur (UN
appel, pas de polling), commune aux payins et aux payouts. Un verdict final est
appliqué via settlement.settle_verdict (settled_by = manual_check)."""

import asyncio
import importlib
import logging

from core import settlement
from core.db import db

log = logging.getLogger("ai_browser2")

_TERMINAL = ("successful", "failed", "cancelled")


def _checker(aggregator: str, kind: str):
    """Fonction de vérification de l'agrégateur : check_deposit_status (payin,
    module status_check) ou check_payout_status (module payout_flow)."""
    if kind == "payout":
        return importlib.import_module(f"aggregators.{aggregator}.payout_flow").check_payout_status
    return importlib.import_module(f"aggregators.{aggregator}.status_check").check_deposit_status


async def check_transaction(tx: dict) -> dict | None:
    """Statut à jour d'une transaction (ligne BD). Déjà finale (ou sans id
    agrégateur) -> statut BD. Sinon un appel agrégateur ; None si injoignable."""
    ref = tx.get("transaction_ref")
    provider_id = tx.get("provider_transaction_id") or ""
    if tx.get("status") in _TERMINAL or not provider_id:
        return {"transaction_id": ref, "status": tx.get("status"),
                "message": tx.get("message"), "final": tx.get("status") in _TERMINAL,
                "source": "database"}
    kind = (tx.get("type") or "payin").lower()
    try:
        check = _checker(tx.get("aggregator", ""), kind)
    except (ModuleNotFoundError, AttributeError):
        return {"transaction_id": ref, "status": tx.get("status"),
                "message": tx.get("message"), "final": False, "source": "database"}
    res = await (check(provider_id) if kind == "payout" else check(provider_id, tx=tx))
    if res is None:
        return None
    payer = res.get("payer") or {}
    if payer.get("network") or payer.get("phone"):
        # Réseau/numéro inconnus à la création (page hébergée) : complétés AVANT
        # le règlement, pour que les frais soient calculés sur le bon réseau.
        await db.fill_payer(provider_id, payer.get("network", ""), payer.get("phone", ""))
    if res["terminal"]:
        await settlement.settle_verdict(provider_id, res["status"], res["message"],
                                        settled_by="manual_check")
    return {"transaction_id": ref, "status": res["status"], "message": res["message"],
            "final": res["terminal"], "source": "aggregator"}


def schedule_check(transaction_ref: str, delay_s: float) -> None:
    """Planifie UNE vérification après `delay_s` secondes (ex. expiration d'une
    page de paiement abandonnée, qui ne déclenche aucun webhook)."""
    async def _run():
        await asyncio.sleep(max(delay_s, 0))
        tx = await db.get_transaction(transaction_ref)
        if tx and tx.get("status") not in _TERMINAL:
            try:
                await check_transaction(tx)
            except Exception as e:  # noqa: BLE001
                log.warning("vérification planifiée %s échouée : %s", transaction_ref, e)
    asyncio.create_task(_run())
