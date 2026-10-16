"""Vérification manuelle du statut d'une transaction chez son agrégateur (UN
appel, pas de polling), commune aux payins et aux payouts. Un verdict final est
appliqué via settlement.settle_verdict (settled_by = manual_check)."""

import importlib

from core import settlement

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
    res = await check(provider_id)
    if res is None:
        return None
    if res["terminal"]:
        await settlement.settle_verdict(provider_id, res["status"], res["message"],
                                        settled_by="manual_check")
    return {"transaction_id": ref, "status": res["status"], "message": res["message"],
            "final": res["terminal"], "source": "aggregator"}
