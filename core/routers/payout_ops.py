"""Routes payout complémentaires : virements groupés (POST /payout/bulk) et
annulation d'un virement encore en file d'attente (POST /payout/{ref}/cancel).

Le bulk réutilise la chaîne de core/routers/payout.py (validation, réservation
du solde, finalisation) ; seul l'appel agrégateur est groupé. Agrégateur sans
bulk -> virements exécutés un par un."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core import aggregator_ops, availability, tenants
from core.auth import AuthContext, require_api_key
from core.config import settings
from core.db import db
from core.routers import payout as p
from core.schemas.operations import BulkPayoutRequest
from core.upstream_errors import UPSTREAM_MESSAGES

log = logging.getLogger("ai_browser2")

router = APIRouter()


def _item(i: int, resp=None, error=None) -> dict:
    if error is not None:
        return {"index": i, "success": False, "status": "rejected", "error": error}
    data = resp if isinstance(resp, dict) else resp.model_dump()
    return {"index": i, **data}


async def _one_by_one(reqs, ctx) -> list:
    out = []
    for i, r in enumerate(reqs):
        try:
            out.append(_item(i, await p._execute_payout(r, ctx.app_id, ctx.api_key_id)))
        except HTTPException as e:
            out.append(_item(i, error=e.detail))
    return out


@router.post(
    "/payout/bulk",
    tags=["payout"],
    summary="Virements groupés (jusqu'à 20)",
    responses={400: {"description": "Agrégateurs différents dans le lot."},
               503: {"description": "Service de paiement amont indisponible."}},
)
async def payout_bulk(body: BulkPayoutRequest, ctx: AuthContext = Depends(require_api_key)):
    """Envoie jusqu'à 20 virements en un seul appel agrégateur. Chaque virement
    est validé et débité séparément : un refus (solde, doublon, opérateur
    indisponible…) n'empêche pas les autres. Résultat par élément (`index`)."""
    reqs = body.payouts
    aggs = {r.aggregator for r in reqs}
    if len(aggs) != 1:
        raise HTTPException(400, {"error": "mixed_aggregators",
                                  "message": "Tous les virements doivent utiliser le même agrégateur."})
    agg = aggs.pop()
    try:
        bulk_fn = aggregator_ops.get(agg, "initiate_payouts_bulk")
    except HTTPException:
        bulk_fn = None
    if settings.mock_payments or bulk_fn is None:
        return {"results": await _one_by_one(reqs, ctx)}

    results: dict[int, dict] = {}
    ready = []  # (index, req, tx_id, balance_after)
    for i, r in enumerate(reqs):
        try:
            p._validate_payout_aggregator_and_network(r)
            await p._guard_duplicate_payout(r)
            await availability.guard(agg, r.account_bank_code, "payout")
            tx_id, bal = await p._prepare_payout(r, ctx.app_id, ctx.api_key_id)
            ready.append((i, r, tx_id, bal))
        except HTTPException as e:
            results[i] = _item(i, error=e.detail)

    if ready:
        try:
            inits = await bulk_fn([r for _, r, _, _ in ready])
        except Exception as e:  # noqa: BLE001 — rien n'est parti : on rembourse tout
            code = getattr(e, "code", None)
            msg = UPSTREAM_MESSAGES.get(code, "Les virements n'ont pas pu être initiés.")
            log.warning("payout bulk: initiation échouée (%s)", e)
            for i, r, tx_id, _ in ready:
                await p._refund(ctx.app_id, r, tx_id)
                await db.update_payout(tx_id, status="failed", success=False, message=msg)
                results[i] = _item(i, error={"code": code or "payout_failed", "message": msg})
        else:
            for (i, r, tx_id, bal), init in zip(ready, inits):
                results[i] = _item(i, await p._apply_init(r, ctx.app_id, tx_id, init, bal, False))
    return {"results": [results[i] for i in sorted(results)]}


@router.post(
    "/payout/{transaction_ref}/cancel",
    tags=["payout"],
    summary="Annuler un virement en file d'attente",
    responses={404: {"description": "Aucun virement pour cette référence."},
               409: {"description": "Virement déjà final."}},
)
async def cancel_payout(transaction_ref: str, ctx: AuthContext = Depends(require_api_key)):
    """Annule un virement que l'agrégateur n'a pas encore envoyé (en file
    d'attente, ex. opérateur lent). Accepté -> le statut `cancelled` arrive par
    webhook et le solde est remboursé. Un virement déjà en cours d'envoi chez
    l'opérateur ne peut plus être annulé (refus de l'agrégateur)."""
    tx = await tenants.get_transaction_scoped(transaction_ref, ctx.app_id)
    if tx is None or (tx.get("type") or "").lower() != "payout":
        raise HTTPException(404, f"No payout found for ref '{transaction_ref}'")
    if tx.get("status") in ("successful", "failed", "cancelled"):
        raise HTTPException(409, {"error": "already_final", "status": tx.get("status")})
    res = await aggregator_ops.call(tx.get("aggregator", ""), "cancel_payout",
                                    tx.get("provider_transaction_id") or "")
    return {"transaction_id": transaction_ref, **res}
