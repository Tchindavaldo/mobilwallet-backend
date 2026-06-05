"""Routes transactions : historique, statut, trace IA, erreurs, déblocage.

Toutes scopées à l'app authentifiée (isolation) : un dev ne voit/agit que sur
les transactions de ses propres apps. Une transaction d'une autre app renvoie
404 (on ne révèle pas son existence).
"""

from fastapi import APIRouter, Depends, HTTPException

from core import tenants
from core.auth import AuthContext, require_api_key
from core.db import db

router = APIRouter()


async def _owned_tx(transaction_ref: str, ctx: AuthContext) -> dict:
    """Récupère une transaction bornée à l'app appelante, ou lève 404."""
    tx = await tenants.get_transaction_scoped(transaction_ref, ctx.app_id)
    if tx is None:
        raise HTTPException(404, f"No transaction found for ref '{transaction_ref}'")
    return tx


@router.get("/transactions", tags=["transactions"], summary="Historique des transactions")
async def list_transactions(limit: int = 50, ctx: AuthContext = Depends(require_api_key)):
    """Dernières transactions de VOTRE app (isolation par clé API)."""
    return {"transactions": await tenants.list_transactions_for_app(ctx.app_id, limit=limit)}


@router.get(
    "/status/{transaction_ref}",
    tags=["transactions"],
    summary="Statut d'une transaction",
    responses={404: {"description": "Aucune transaction pour cette référence."}},
)
async def transaction_status(transaction_ref: str, ctx: AuthContext = Depends(require_api_key)):
    """Récupère une transaction de votre app par sa référence."""
    return await _owned_tx(transaction_ref, ctx)


@router.get(
    "/transactions/{transaction_ref}/trace",
    tags=["transactions"],
    summary="Trace IA d'une transaction (mode browser)",
    responses={404: {"description": "Aucune transaction pour cette référence."}},
)
async def transaction_trace(transaction_ref: str, ctx: AuthContext = Depends(require_api_key)):
    """Trace tour-par-tour de l'agent IA pour une transaction browser de votre app.

    Chaque entrée : ce que l'IA a vu (url, nb d'éléments), sa pensée, les actions
    jouées, et si l'objectif a été atteint. Vide pour un paiement en mode replay.
    """
    tx = await _owned_tx(transaction_ref, ctx)
    traces = await db.get_traces(tx["id"])
    return {"transaction_ref": transaction_ref, "turns": len(traces), "trace": traces}


@router.get(
    "/transactions/{transaction_ref}/errors",
    tags=["transactions"],
    summary="Erreurs détaillées d'une transaction (browser/replay)",
    responses={404: {"description": "Aucune transaction pour cette référence."}},
)
async def transaction_errors(transaction_ref: str, ctx: AuthContext = Depends(require_api_key)):
    """Erreurs détaillées d'une transaction de votre app.

    Chaque entrée précise le moteur (`engine`: browser/replay) et la SOURCE
    (`source`: ai / browser / transaction / replay) pour savoir exactement où ça
    a cassé, plus la catégorie, le message clair et les détails. Vide si la
    transaction a réussi.
    """
    tx = await _owned_tx(transaction_ref, ctx)
    errors = await db.get_errors(tx["id"])
    return {"transaction_ref": transaction_ref, "count": len(errors), "errors": errors}


@router.post(
    "/transactions/{tx_id}/cancel",
    tags=["transactions"],
    summary="Débloquer une transaction pending",
    responses={
        404: {"description": "Aucune transaction 'pending' (de votre app) avec cet id."},
        503: {"description": "Supabase non configuré."},
    },
)
async def cancel_transaction(tx_id: int, ctx: AuthContext = Depends(require_api_key)):
    """Force une transaction restée **pending** à **cancelled** (de votre app).

    Sert à débloquer une transaction coincée (ex: serveur interrompu avant le
    verdict) qui empêcherait un nouveau paiement sur le même numéro
    (`409 pending_exists`). N'agit que sur une ligne encore `pending` de votre
    app — ne réécrit jamais un verdict déjà acté.
    """
    if not db.enabled:
        raise HTTPException(503, "Supabase non configuré.")
    row = await db.cancel_pending(tx_id, app_id=ctx.app_id)
    if row is None:
        raise HTTPException(404, f"Aucune transaction 'pending' (de votre app) avec l'id {tx_id}.")
    return {"cancelled": row}
