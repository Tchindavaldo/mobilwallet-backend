"""Contrôles d'un payout AVANT tout débit (utilisés par core/routers/payout.py et
payout_ops.py) : agrégateur et réseau stricts, solde opérateur à débiter
(`source_network`), anti-doublon par bénéficiaire.
"""

from fastapi import HTTPException

from core import registry
from core.db import db
from core.schemas.payout import PayoutRequest

# Codes accountBankCode acceptés AU PAYOUT — STRICT, aucune conversion ni variante.
# Le client doit envoyer EXACTEMENT l'une de ces valeurs (DigiKUNTZ les exige telles
# quelles). Différent du payin (qui tolère 'orange' -> 'Orangemoney').
_PAYOUT_BANK_CODES = ("ORANGEMONEY", "MTN")

# Agrégateurs supportant le payout -> module exposant initiate_payout /
# poll_payout_status / friendly_msg / PayoutUpstreamError (même interface).
_PAYOUT_FLOWS = {
    "digikuntz": "aggregators.digikuntz.payout_flow",
    "pawapay": "aggregators.pawapay.payout_flow",
}


def _validate_payout_aggregator_and_network(req: PayoutRequest) -> None:
    """Valide l'agrégateur et exige un account_bank_code STRICT (ORANGEMONEY|MTN),
    sans aucune tolérance/conversion. Lève 404/400/422 sinon."""
    if req.aggregator not in registry.names():
        raise HTTPException(
            404,
            {"error": "unknown_aggregator",
             "message": f"Agrégateur '{req.aggregator}' inconnu.",
             "supported_aggregators": registry.names()},
        )
    if req.aggregator not in _PAYOUT_FLOWS:
        raise HTTPException(
            400,
            {"error": "payout_unsupported",
             "message": f"L'agrégateur '{req.aggregator}' ne supporte pas les payouts."},
        )
    if req.account_bank_code not in _PAYOUT_BANK_CODES:
        raise HTTPException(
            422,
            {"error": "invalid_network",
             "message": f"account_bank_code doit être exactement l'un de "
                        f"{list(_PAYOUT_BANK_CODES)} (reçu : '{req.account_bank_code}').",
             "aggregator": req.aggregator,
             "supported_account_bank_codes": list(_PAYOUT_BANK_CODES)},
        )


async def _require_source_network(req: PayoutRequest, app_id: int) -> None:
    """Exige le solde opérateur à débiter (`source_network`, même liste stricte
    que account_bank_code). Absent ou invalide : 422 avec les soldes opérateur
    de l'app chez cet agrégateur, rien n'est débité."""
    if req.source_network in _PAYOUT_BANK_CODES:
        return
    balances = (await db.balances_by_network(app_id)).get(req.aggregator, {})
    raise HTTPException(
        422,
        {"error": "source_network_required",
         "message": "Choisissez le solde opérateur à débiter (MTN ou Orange).",
         "aggregator": req.aggregator,
         "supported_source_networks": list(_PAYOUT_BANK_CODES),
         "network": {"mtn": balances.get("mtn", 0), "orange": balances.get("orange", 0)},
         "currency": req.currency},
    )


async def _guard_duplicate_payout(req: PayoutRequest) -> None:
    """Garde anti-doublon : au plus un payout 'pending' par bénéficiaire."""
    last = await db.last_payout_for_account(req.aggregator, req.account_number)
    if not last:
        return
    if (last.get("status") or "").lower() == "pending":
        raise HTTPException(
            409,
            {
                "error": "pending_exists",
                "message": (
                    f"Un virement est déjà en cours (pending) vers le compte "
                    f"{req.account_number}. Veuillez attendre son aboutissement."
                ),
                "aggregator": req.aggregator,
                "account_number": req.account_number,
                "transaction_id": last.get("id"),
            },
        )
